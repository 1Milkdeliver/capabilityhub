"""Reusable local catalog generations for MCP, CLI, and dashboard surfaces."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic, time

from capabilityhub.degraded import Dependency, DependencyObservation, DependencyStatus
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.local_catalog import discover_local_catalog, local_catalog_fingerprint
from capabilityhub.models import CapabilityKind, JsonValue
from capabilityhub.providers.base import CapabilityProvider
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.update_store import SQLiteUpdateStore


@dataclass(frozen=True, slots=True)
class LocalCatalogGeneration:
    """One complete catalog generation and its safe inventory envelope."""

    registry: CapabilityRegistry
    providers: tuple[CapabilityProvider, ...]
    inventory: dict[str, JsonValue]
    observed_at: float
    observation_ttl_seconds: float

    def inventory_json(self) -> dict[str, JsonValue]:
        """Return an isolated JSON copy safe for callers to mutate."""

        return deepcopy(self.inventory)


class LocalCatalogMonitor:
    """Single-flight local refresh with last-complete-snapshot fallback."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        project: Path | None = None,
        refresh_interval_seconds: float = 0.25,
    ) -> None:
        if refresh_interval_seconds < 0:
            raise ValueError("refresh_interval_seconds must be non-negative")
        self._home = (home or Path.home()).resolve()
        self._project = (project or Path.cwd()).resolve()
        self._refresh_interval_seconds = refresh_interval_seconds
        self._lock = RLock()
        self._fingerprint = ""
        self._generation = 0
        self._next_check = 0.0
        self._snapshot: LocalCatalogGeneration | None = None

    @property
    def project(self) -> Path:
        return self._project

    @property
    def home(self) -> Path:
        return self._home

    def snapshot(self, *, force: bool = False) -> LocalCatalogGeneration:
        """Return a complete current generation, refreshing only after input changes."""

        with self._lock:
            now = monotonic()
            if not force and self._snapshot is not None and now < self._next_check:
                return self._snapshot
            try:
                fingerprint = local_catalog_fingerprint(home=self._home, project=self._project)
                if self._snapshot is None or fingerprint != self._fingerprint:
                    refreshed = self._build_snapshot()
                    self._snapshot = refreshed
                    self._fingerprint = fingerprint
                else:
                    self._snapshot = LocalCatalogGeneration(
                        self._snapshot.registry,
                        self._snapshot.providers,
                        self._snapshot.inventory,
                        time(),
                        self._snapshot.observation_ttl_seconds,
                    )
            except Exception:
                self._next_check = monotonic() + self._refresh_interval_seconds
                if self._snapshot is None:
                    raise
                inventory = dict(self._snapshot.inventory)
                inventory["last_refresh_error_code"] = "catalog_refresh_failed"
                inventory["status"] = "stale"
                return LocalCatalogGeneration(
                    self._snapshot.registry,
                    self._snapshot.providers,
                    inventory,
                    self._snapshot.observed_at,
                    self._snapshot.observation_ttl_seconds,
                )
            self._next_check = monotonic() + self._refresh_interval_seconds
            return self._snapshot

    def _build_snapshot(self) -> LocalCatalogGeneration:
        catalog = discover_local_catalog(home=self._home, project=self._project)
        registry = CapabilityRegistry()
        registration_conflicts = 0
        for manifest in sorted(catalog.manifests, key=lambda item: item.identity.revision):
            try:
                registry.register(manifest)
            except CapabilityHubError:
                registration_conflicts += 1

        update_pointers: dict[str, str] = {}
        state_path = self._project / ".capabilityhub" / "state.sqlite3"
        if state_path.is_file():
            update_pointers = SQLiteUpdateStore(state_path).active_pointers()
        for coordinate, revision in update_pointers.items():
            manifest = registry.revision(revision)
            if manifest.identity.coordinate != coordinate:
                raise CapabilityHubError(
                    code="update_pointer_invalid",
                    category=ErrorCategory.REFERENCE,
                    safe_message="A persisted update pointer does not match the catalog.",
                )

        pending = [
            manifest
            for manifest in catalog.manifests
            if manifest.identity.revision in registry.revisions
            and manifest.identity.coordinate not in catalog.inactive_coordinates
            and update_pointers.get(manifest.identity.coordinate, manifest.identity.revision)
            == manifest.identity.revision
        ]
        activation_errors: dict[str, CapabilityHubError] = {}
        while pending:
            remaining = []
            progress = False
            for manifest in pending:
                try:
                    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
                except CapabilityHubError as error:
                    activation_errors[manifest.identity.revision] = error
                    remaining.append(manifest)
                else:
                    activation_errors.pop(manifest.identity.revision, None)
                    progress = True
            if not progress:
                break
            pending = remaining

        active_by_kind: dict[str, JsonValue] = {kind.value: 0 for kind in CapabilityKind}
        for revision in registry.activations.values():
            kind = registry.revision(revision).kind.value
            count = active_by_kind[kind]
            assert isinstance(count, int)
            active_by_kind[kind] = count + 1
        discovered_total = len(registry.revisions)
        active_total = len(registry.activations)
        registry.freeze()
        self._generation += 1
        conflict_count = catalog.conflict_count + registration_conflicts
        dependency_inactive = sum(
            error.category.value == "dependency" for error in activation_errors.values()
        )
        activation_conflict = sum(
            error.category.value == "conflict" for error in activation_errors.values()
        )
        activation_failed = len(activation_errors) - dependency_inactive - activation_conflict
        excluded_by_reason: dict[str, JsonValue] = {
            "activation_conflict": activation_conflict,
            "activation_failed": activation_failed,
            "configured_disabled": len(
                catalog.inactive_coordinates
                - catalog.controlled_disabled_coordinates
                - catalog.quarantined_coordinates
            ),
            "control_disabled": len(catalog.controlled_disabled_coordinates),
            "dependency_inactive": dependency_inactive,
            "duplicate_identical": catalog.duplicate_count,
            "invalid_manifest": catalog.invalid_count,
            "path_escape": catalog.skipped_count,
            "registration_conflict": registration_conflicts,
            "quarantined": len(catalog.quarantined_coordinates),
            "shadowed_conflict": catalog.conflict_count,
        }
        partial = any(
            value
            for key, value in excluded_by_reason.items()
            if key
            not in {
                "configured_disabled",
                "control_disabled",
                "duplicate_identical",
                "quarantined",
            }
            and isinstance(value, int)
        )
        inventory: dict[str, JsonValue] = {
            "active_by_kind": active_by_kind,
            "active_total": active_total,
            "conflict_count": conflict_count,
            "discovered_total": discovered_total,
            "duplicate_count": catalog.duplicate_count,
            "excluded_by_reason": excluded_by_reason,
            "generation": self._generation,
            "inactive_count": discovered_total - active_total,
            "invalid_count": catalog.invalid_count,
            "last_refresh_error_code": None,
            "skipped_count": catalog.skipped_count,
            "status": "partial" if partial else "fresh",
        }
        return LocalCatalogGeneration(
            registry,
            (*catalog.skill_providers, *catalog.configured_providers),
            inventory,
            time(),
            max(1.0, self._refresh_interval_seconds * 2),
        )


def local_dependency_observations(
    generation: LocalCatalogGeneration,
    *,
    policy_available: bool,
    provider_name: str | None = None,
    providers: Iterable[CapabilityProvider] | None = None,
    observed_at: float | None = None,
) -> tuple[DependencyObservation, ...]:
    """Build location-free dependency evidence from one real local generation."""

    now = time() if observed_at is None else observed_at
    catalog_status = (
        DependencyStatus.STALE
        if generation.inventory.get("status") == "stale"
        else DependencyStatus.AVAILABLE
    )
    provider_status = DependencyStatus.UNKNOWN
    if provider_name is not None:
        available_providers = generation.providers if providers is None else tuple(providers)
        provider_status = (
            DependencyStatus.AVAILABLE
            if any(provider.name == provider_name for provider in available_providers)
            else DependencyStatus.UNAVAILABLE
        )
    return (
        DependencyObservation(
            Dependency.REGISTRY,
            catalog_status,
            generation.observed_at,
            generation.observation_ttl_seconds,
        ),
        DependencyObservation(
            Dependency.INDEX,
            catalog_status,
            generation.observed_at,
            generation.observation_ttl_seconds,
        ),
        DependencyObservation(
            Dependency.POLICY,
            DependencyStatus.AVAILABLE if policy_available else DependencyStatus.UNKNOWN,
            now,
            generation.observation_ttl_seconds,
        ),
        DependencyObservation(
            Dependency.PROVIDER,
            provider_status,
            now,
            generation.observation_ttl_seconds,
        ),
    )
