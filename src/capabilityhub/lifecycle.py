"""Staged capability update coordination without artifact download or execution."""

from __future__ import annotations

from collections.abc import Mapping

from .registry import CapabilityRegistry
from .update_store import RevisionPin, SQLiteUpdateStore, UpdateState


class StagedUpdateManager:
    """Validate immutable registry revisions around an atomic SQLite pointer store."""

    def __init__(self, *, registry: CapabilityRegistry, store: SQLiteUpdateStore) -> None:
        self.registry = registry
        self.store = store

    def stage(
        self,
        revision: str,
        *,
        expected_active_revision: str | None,
    ) -> UpdateState:
        """Stage a registered revision without fetching or executing its artifact."""

        manifest = self.registry.revision(revision)
        return self.store.stage(
            manifest.identity.coordinate,
            revision,
            expected_active_revision=expected_active_revision,
        )

    def bootstrap_active(self, coordinate: str, revision: str) -> UpdateState:
        manifest = self.registry.revision(revision)
        if manifest.identity.coordinate != coordinate:
            raise ValueError("bootstrap coordinate does not match revision")
        return self.store.bootstrap_active(coordinate, revision)

    def record_health(self, revision: str, *, passed: bool) -> UpdateState:
        """Record an externally obtained health result; this performs no health execution."""

        manifest = self.registry.revision(revision)
        return self.store.record_health(
            manifest.identity.coordinate,
            revision,
            passed=passed,
        )

    def activate(
        self,
        revision: str,
        *,
        expected_active_revision: str | None,
    ) -> UpdateState:
        manifest = self.registry.revision(revision)
        return self.store.activate(
            manifest.identity.coordinate,
            revision,
            expected_active_revision=expected_active_revision,
            validate=self._validate_pointers,
        )

    def rollback(self, coordinate: str, *, expected_active_revision: str) -> UpdateState:
        return self.store.rollback(
            coordinate,
            expected_active_revision=expected_active_revision,
            validate=self._validate_pointers,
        )

    def state(self, coordinate: str) -> UpdateState:
        return self.store.state(coordinate)

    def states(self, *, limit: int = SQLiteUpdateStore.MAX_STATE_ROWS) -> tuple[UpdateState, ...]:
        return self.store.states(limit=limit)

    def pin_active(self, coordinate: str, pin_id: str) -> RevisionPin:
        return self.store.pin_active(coordinate, pin_id)

    def pins(self, coordinate: str | None = None) -> tuple[RevisionPin, ...]:
        return self.store.pins(coordinate)

    def release_pin(self, pin_id: str) -> bool:
        return self.store.release_pin(pin_id)

    def _validate_pointers(self, pointers: Mapping[str, str]) -> None:
        combined = dict(self.registry.activations)
        combined.update(pointers)
        for coordinate, revision in combined.items():
            manifest = self.registry.revision(revision)
            if manifest.identity.coordinate != coordinate:
                raise ValueError("active pointer coordinate does not match its revision")
        self.registry.validate_staged(combined.values())
