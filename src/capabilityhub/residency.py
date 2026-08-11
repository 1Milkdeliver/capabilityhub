"""Bounded inventory for material already disclosed to a model context."""

from __future__ import annotations

from dataclasses import dataclass

from capabilityhub.errors import CapabilityHubError, ErrorCategory


@dataclass(slots=True)
class ResidentSection:
    key: str
    revision: str
    section: str
    portable_tokens: int
    reload_cost: int = 1
    reuse_score: int = 1
    sensitive: bool = False
    pinned: bool = False
    last_access: int = 0


@dataclass(frozen=True, slots=True)
class Eviction:
    key: str
    portable_tokens: int
    reason: str


class ContextInventory:
    """Tracks context residency separately from the server-side artifact cache."""

    def __init__(self, max_portable_tokens: int) -> None:
        if max_portable_tokens <= 0:
            raise ValueError("max_portable_tokens must be positive")
        self.max_portable_tokens = max_portable_tokens
        self._entries: dict[str, ResidentSection] = {}
        self._clock = 0

    @property
    def used_portable_tokens(self) -> int:
        return sum(entry.portable_tokens for entry in self._entries.values())

    @property
    def entries(self) -> tuple[ResidentSection, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    def add(self, entry: ResidentSection) -> tuple[Eviction, ...]:
        if not entry.key or entry.portable_tokens < 0:
            raise ValueError("resident section key and token size must be valid")
        previous = self._entries.pop(entry.key, None)
        self._touch(entry)
        self._entries[entry.key] = entry
        try:
            return self._evict_to_limit(exclude={entry.key})
        except CapabilityHubError:
            self._entries.pop(entry.key, None)
            if previous is not None:
                self._entries[previous.key] = previous
            raise

    def access(self, key: str) -> ResidentSection:
        try:
            entry = self._entries[key]
        except KeyError as error:
            raise KeyError(f"unknown resident section: {key}") from error
        self._touch(entry)
        return entry

    def pin(self, key: str, value: bool = True) -> None:
        self.access(key).pinned = value

    def remove(self, key: str) -> Eviction:
        entry = self._entries.pop(key)
        return Eviction(key, entry.portable_tokens, "explicit")

    def _touch(self, entry: ResidentSection) -> None:
        self._clock += 1
        entry.last_access = self._clock

    def _evict_to_limit(self, exclude: set[str]) -> tuple[Eviction, ...]:
        evictions: list[Eviction] = []
        while self.used_portable_tokens > self.max_portable_tokens:
            candidates = [
                entry
                for entry in self._entries.values()
                if not entry.pinned and entry.key not in exclude
            ]
            if not candidates:
                raise CapabilityHubError(
                    code="context_budget_exhausted",
                    category=ErrorCategory.BUDGET,
                    safe_message="The context budget cannot fit all pinned sections.",
                )
            victim = min(candidates, key=self._eviction_order)
            self._entries.pop(victim.key)
            evictions.append(Eviction(victim.key, victim.portable_tokens, "budget_pressure"))
        return tuple(evictions)

    @staticmethod
    def _eviction_order(entry: ResidentSection) -> tuple[float, int, str]:
        sensitivity_penalty = 2 if entry.sensitive else 1
        value = (entry.reuse_score * entry.reload_cost) / max(
            1, entry.portable_tokens * sensitivity_penalty
        )
        return value, entry.last_access, entry.key
