"""A deterministic, in-memory registry of immutable capability revisions.

This module is control-plane only: it stores and validates manifests and never
imports, initializes, or executes provider code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import CapabilityKind, CapabilityManifest, DependencySpec
from capabilityhub.projections import ProjectionPolicy, ProjectionResolution, resolve_projections

_SEMVER_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")


class CapabilityRegistry:
    """Revision store with deterministic validation and activation pointers."""

    def __init__(self, *, projection_policy: ProjectionPolicy | None = None) -> None:
        self._revisions: dict[str, CapabilityManifest] = {}
        self._by_coordinate: dict[str, list[str]] = {}
        self._active: dict[str, str] = {}
        self._by_kind: dict[CapabilityKind, set[str]] = {kind: set() for kind in CapabilityKind}
        self._frozen = False
        self._projection_policy = projection_policy or ProjectionPolicy("isolate")
        self._projection_resolution = resolve_projections((), self._projection_policy)

    def freeze(self) -> None:
        """Prevent further revision or activation mutation."""

        self._frozen = True

    @property
    def revisions(self) -> Mapping[str, CapabilityManifest]:
        return MappingProxyType(dict(self._revisions))

    @property
    def activations(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self._active))

    @property
    def projection_resolution(self) -> ProjectionResolution:
        return self._projection_resolution

    @property
    def active_digest(self) -> str:
        payload = json.dumps(
            dict(sorted(self._active.items())), sort_keys=True, separators=(",", ":")
        ).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def register(self, manifest: CapabilityManifest) -> CapabilityManifest:
        """Store one immutable revision. Dependency activation is a separate step."""

        return self.register_many((manifest,))[0]

    def register_many(
        self, manifests: Iterable[CapabilityManifest]
    ) -> tuple[CapabilityManifest, ...]:
        """Atomically store revisions after rejecting duplicate revision identities."""

        self._ensure_mutable()
        additions = tuple(manifests)
        staged = dict(self._revisions)
        for manifest in additions:
            if not isinstance(manifest, CapabilityManifest):
                raise _input(
                    "invalid_manifest", "Registry accepts CapabilityManifest instances only."
                )
            revision = manifest.identity.revision
            existing = staged.get(revision)
            if existing is not None and existing != manifest:
                raise _conflict(
                    "duplicate_revision",
                    "A revision identity cannot be overwritten.",
                    revision=revision,
                )
            staged[revision] = manifest

        projection_resolution = resolve_projections(
            self._selected_manifest_values(staged),
            self._projection_policy,
        )

        # Do not require dependencies at install time: installers may stage a graph
        # incrementally. The graph can be checked with validate_staged, while active
        # pointers always require a complete, conflict-free active closure.
        self._revisions = staged
        self._rebuild_indexes()
        self._projection_resolution = projection_resolution
        return additions

    def revision(self, revision: str) -> CapabilityManifest:
        try:
            return self._revisions[revision]
        except KeyError as error:
            raise _reference(
                "unknown_revision", "Capability revision is not registered.", revision=revision
            ) from error

    def revisions_for(self, coordinate: str) -> tuple[CapabilityManifest, ...]:
        return tuple(self._revisions[item] for item in self._by_coordinate.get(coordinate, ()))

    def by_kind(self, kind: CapabilityKind | str) -> tuple[CapabilityManifest, ...]:
        parsed_kind = _kind(kind)
        return tuple(self._revisions[item] for item in sorted(self._by_kind[parsed_kind]))

    def active(self, coordinate: str) -> CapabilityManifest:
        try:
            return self._revisions[self._active[coordinate]]
        except KeyError as error:
            raise _reference(
                "inactive_capability", "Capability has no active revision.", coordinate=coordinate
            ) from error

    def activate(self, coordinate: str, revision: str | None = None) -> CapabilityManifest:
        """Atomically point a coordinate at a validated, immutable revision."""

        self._ensure_mutable()
        chosen_revision = revision if revision is not None else self._active_candidate(coordinate)
        manifest = self.revision(chosen_revision)
        if manifest.identity.coordinate != coordinate:
            raise _input(
                "activation_coordinate_mismatch", "Activation coordinate does not match revision."
            )
        candidate = dict(self._active)
        candidate[coordinate] = chosen_revision
        resolution = self._projection_resolution
        if coordinate in resolution.excluded_coordinates:
            raise _conflict(
                "projection_coordinate_excluded",
                "Capability was excluded by the active projection policy.",
                projection_digest=resolution.digest,
            )
        candidate = {
            item_coordinate: item_revision
            for item_coordinate, item_revision in candidate.items()
            if item_coordinate not in resolution.excluded_coordinates
        }
        self._validate_active(candidate)
        self._active = candidate
        self._projection_resolution = resolution
        return manifest

    def deactivate(self, coordinate: str) -> None:
        self._ensure_mutable()
        if coordinate not in self._active:
            raise _reference(
                "inactive_capability", "Capability has no active revision.", coordinate=coordinate
            )
        candidate = dict(self._active)
        del candidate[coordinate]
        self._validate_active(candidate)
        self._active = candidate

    def validate_staged(self, revisions: Iterable[str] | None = None) -> None:
        """Validate a staged graph without mutating activations.

        If multiple revisions of one coordinate are selected, the highest stable
        version (then revision string) is used as the dependency target.
        """

        selected = self._selected_revisions(revisions)
        self._validate_dependencies(selected)
        self._validate_conflicts(selected)
        resolve_projections(selected.values(), self._projection_policy)

    def validate_active(self) -> None:
        """Validate current pointers, primarily useful for import/recovery checks."""

        self._validate_active(self._active)

    def _validate_active(self, active: Mapping[str, str]) -> None:
        selected = {coordinate: self.revision(revision) for coordinate, revision in active.items()}
        self._validate_dependencies(selected, require_active=True)
        self._validate_conflicts(selected)
        self._resolve_active_projections(active)

    def _resolve_active_projections(
        self, active: Mapping[str, str]
    ) -> ProjectionResolution:
        return resolve_projections(
            (self.revision(revision) for revision in active.values()),
            self._projection_policy,
        )

    def _selected_revisions(self, revisions: Iterable[str] | None) -> dict[str, CapabilityManifest]:
        source = tuple(self._revisions) if revisions is None else tuple(revisions)
        candidates: dict[str, list[CapabilityManifest]] = {}
        for revision in source:
            manifest = self.revision(revision)
            candidates.setdefault(manifest.identity.coordinate, []).append(manifest)
        return {
            coordinate: max(items, key=_revision_order)
            for coordinate, items in sorted(candidates.items())
        }

    @staticmethod
    def _selected_manifest_values(
        revisions: Mapping[str, CapabilityManifest],
    ) -> tuple[CapabilityManifest, ...]:
        candidates: dict[str, list[CapabilityManifest]] = {}
        for manifest in revisions.values():
            candidates.setdefault(manifest.identity.coordinate, []).append(manifest)
        return tuple(
            max(items, key=_revision_order)
            for _coordinate, items in sorted(candidates.items())
        )

    def _validate_dependencies(
        self, selected: Mapping[str, CapabilityManifest], *, require_active: bool = False
    ) -> None:
        graph: dict[str, tuple[str, ...]] = {}
        for coordinate in sorted(selected):
            manifest = selected[coordinate]
            targets: list[str] = []
            for dependency in sorted(manifest.dependencies, key=lambda item: item.coordinate):
                target = selected.get(dependency.coordinate)
                if target is None:
                    if dependency.optional:
                        continue
                    state = "active" if require_active else "staged"
                    raise _dependency(
                        "missing_dependency",
                        f"Required dependency is not {state}.",
                        capability=coordinate,
                        dependency=dependency.coordinate,
                    )
                if not _satisfies(target.identity.version, dependency):
                    if dependency.optional:
                        continue
                    raise _dependency(
                        "unsatisfied_dependency_version",
                        "Required dependency does not satisfy its version constraint.",
                        capability=coordinate,
                        dependency=dependency.coordinate,
                        constraint=dependency.version_constraint,
                        found=target.identity.version,
                    )
                targets.append(dependency.coordinate)
            graph[coordinate] = tuple(targets)
        self._raise_on_cycle(graph)

    def _validate_conflicts(self, selected: Mapping[str, CapabilityManifest]) -> None:
        claims: dict[tuple[str, str], str] = {}
        for coordinate in sorted(selected):
            for conflict in sorted(
                selected[coordinate].conflicts, key=lambda item: (item.conflict_type, item.value)
            ):
                key = (conflict.conflict_type, conflict.value)
                previous = claims.get(key)
                if previous is not None and previous != coordinate:
                    raise _conflict(
                        "capability_conflict",
                        "Active capabilities claim the same exclusive conflict value.",
                        conflict_type=conflict.conflict_type,
                        value_digest=_safe_conflict_digest(
                            conflict.conflict_type,
                            conflict.value,
                        ),
                        coordinates=(previous, coordinate),
                    )
                claims[key] = coordinate

    @staticmethod
    def _raise_on_cycle(graph: Mapping[str, tuple[str, ...]]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()
        path: list[str] = []

        def visit(node: str) -> None:
            if node in visiting:
                start = path.index(node)
                cycle = (*path[start:], node)
                raise _dependency("dependency_cycle", "Dependency cycle detected.", cycle=cycle)
            if node in visited:
                return
            visiting.add(node)
            path.append(node)
            for target in graph[node]:
                visit(target)
            path.pop()
            visiting.remove(node)
            visited.add(node)

        for node in sorted(graph):
            visit(node)

    def _active_candidate(self, coordinate: str) -> str:
        revisions = self._by_coordinate.get(coordinate)
        if not revisions:
            raise _reference(
                "unknown_coordinate",
                "Capability coordinate is not registered.",
                coordinate=coordinate,
            )
        return max(revisions, key=lambda item: _revision_order(self._revisions[item]))

    def _rebuild_indexes(self) -> None:
        coordinates: dict[str, list[str]] = {}
        kinds: dict[CapabilityKind, set[str]] = {kind: set() for kind in CapabilityKind}
        for revision, manifest in self._revisions.items():
            coordinates.setdefault(manifest.identity.coordinate, []).append(revision)
            kinds[manifest.kind].add(revision)
        self._by_coordinate = {
            coordinate: sorted(items, key=lambda item: _revision_order(self._revisions[item]))
            for coordinate, items in coordinates.items()
        }
        self._by_kind = kinds

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise _conflict("registry_frozen", "This registry snapshot is read-only.")


def _kind(kind: CapabilityKind | str) -> CapabilityKind:
    try:
        return CapabilityKind(kind)
    except ValueError as error:
        raise _input("invalid_capability_kind", "Unknown capability kind.") from error


def _revision_order(manifest: CapabilityManifest) -> tuple[tuple[int, int, int, str], str]:
    version = manifest.identity.version
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        parsed = (-1, -1, -1, version)
    else:
        parsed = (
            int(match.group(1) or 0),
            int(match.group(2) or 0),
            int(match.group(3) or 0),
            version,
        )
    return parsed, manifest.identity.revision


def _satisfies(version: str, dependency: DependencySpec) -> bool:
    constraint = dependency.version_constraint.strip()
    if constraint in ("", "*"):
        return True
    candidate = _version_number(version)
    if candidate is None:
        return constraint == version
    for expression in constraint.split():
        if expression.startswith("^"):
            lower = _version_number(expression[1:])
            if lower is None or candidate < lower or candidate[0] != lower[0]:
                return False
        elif expression.startswith("~"):
            lower = _version_number(expression[1:])
            if lower is None or candidate < lower or candidate[:2] != lower[:2]:
                return False
        elif expression.startswith((">=", "<=", ">", "<")):
            operator = (
                ">="
                if expression.startswith(">=")
                else "<="
                if expression.startswith("<=")
                else expression[0]
            )
            target = _version_number(expression[len(operator) :])
            if (
                target is None
                or not {
                    ">=": candidate >= target,
                    "<=": candidate <= target,
                    ">": candidate > target,
                    "<": candidate < target,
                }[operator]
            ):
                return False
        elif candidate != _version_number(expression):
            return False
    return True


def _version_number(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.fullmatch(value)
    if match is None:
        return None
    return (
        int(match.group(1) or 0),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
    )


def _input(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INPUT, safe_message=message)


def _reference(code: str, message: str, **details: object) -> CapabilityHubError:
    return CapabilityHubError(
        code=code, category=ErrorCategory.REFERENCE, safe_message=message, details=details
    )


def _dependency(code: str, message: str, **details: object) -> CapabilityHubError:
    return CapabilityHubError(
        code=code, category=ErrorCategory.DEPENDENCY, safe_message=message, details=details
    )


def _conflict(code: str, message: str, **details: object) -> CapabilityHubError:
    return CapabilityHubError(
        code=code, category=ErrorCategory.CONFLICT, safe_message=message, details=details
    )


def _safe_conflict_digest(conflict_type: str, value: str) -> str:
    material = f"capabilityhub-conflict-v1\0{conflict_type}\0{value}".encode()
    return "sha256:" + hashlib.sha256(material).hexdigest()
