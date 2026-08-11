"""Reusable local catalog generations for MCP, CLI, and dashboard surfaces."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic

from capabilityhub.errors import CapabilityHubError
from capabilityhub.local_catalog import discover_local_catalog, local_catalog_fingerprint
from capabilityhub.models import CapabilityKind, JsonValue
from capabilityhub.providers.base import CapabilityProvider
from capabilityhub.registry import CapabilityRegistry


@dataclass(frozen=True, slots=True)
class LocalCatalogGeneration:
    """One complete catalog generation and its safe inventory envelope."""

    registry: CapabilityRegistry
    providers: tuple[CapabilityProvider, ...]
    inventory: dict[str, JsonValue]

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

        pending = [
            manifest
            for manifest in catalog.manifests
            if manifest.identity.revision in registry.revisions
            and manifest.identity.coordinate not in catalog.inactive_coordinates
        ]
        while pending:
            remaining = []
            progress = False
            for manifest in pending:
                try:
                    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
                except CapabilityHubError:
                    remaining.append(manifest)
                else:
                    progress = True
            if not progress:
                break
            pending = remaining

        active_by_kind: dict[str, JsonValue] = {
            kind.value: 0 for kind in CapabilityKind
        }
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
        excluded_by_reason: dict[str, JsonValue] = {
            "configured_disabled": len(catalog.inactive_coordinates),
            "dependency_inactive": len(pending),
            "duplicate_identical": catalog.duplicate_count,
            "invalid_manifest": catalog.invalid_count,
            "path_escape": catalog.skipped_count,
            "registration_conflict": registration_conflicts,
            "shadowed_conflict": catalog.conflict_count,
        }
        partial = any(
            value
            for key, value in excluded_by_reason.items()
            if key not in {"configured_disabled", "duplicate_identical"}
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
        return LocalCatalogGeneration(registry, catalog.skill_providers, inventory)
