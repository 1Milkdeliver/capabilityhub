"""Hierarchical hard-cap budget accounting.

Reservations protect capacity before work starts. Reconciliation atomically replaces
the reservation with the actual charge at every level in the hierarchy.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from uuid import uuid4

from .errors import CapabilityHubError, ErrorCategory


class BudgetExceeded(CapabilityHubError):
    """A reservation or reconciliation would exceed a hard cap."""

    def __init__(
        self,
        *,
        scope: str,
        counter: str,
        limit: int,
        requested_total: int,
    ) -> None:
        super().__init__(
            code="budget_exhausted",
            category=ErrorCategory.BUDGET,
            safe_message=f"The {counter!r} budget is exhausted for scope {scope!r}.",
            retryable=False,
            details={
                "scope": scope,
                "counter": counter,
                "limit": limit,
                "requested_total": requested_total,
            },
        )


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Immutable view of one level of a budget hierarchy."""

    scope: str
    limits: Mapping[str, int]
    used: Mapping[str, int]
    reserved: Mapping[str, int]
    remaining: Mapping[str, int]


def _validate_amounts(
    amounts: Mapping[str, int], *, label: str, drop_zero: bool = True
) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for counter, value in amounts.items():
        if not isinstance(counter, str) or not counter:
            raise ValueError(f"{label} counter names must be non-empty strings")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label}[{counter!r}] must be a non-negative integer")
        if value or not drop_zero:
            normalized[counter] = value
    return normalized


class BudgetLedger:
    """A thread-safe budget node whose charges also apply to every ancestor.

    Missing limits mean that the counter is unlimited at that particular scope. A
    child can therefore impose a task cap while its parent imposes a tenant cap.
    """

    def __init__(
        self,
        scope: str,
        limits: Mapping[str, int],
        *,
        parent: BudgetLedger | None = None,
    ) -> None:
        if not scope:
            raise ValueError("scope must be non-empty")
        normalized = _validate_amounts(limits, label="limits", drop_zero=False)
        if parent is self:
            raise ValueError("a budget ledger cannot parent itself")
        self.scope = scope
        self.parent = parent
        self._limits = normalized
        self._used: dict[str, int] = {}
        self._reserved: dict[str, int] = {}
        self._lock = RLock()

    @property
    def limits(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self._limits))

    def create_child(self, scope: str, limits: Mapping[str, int]) -> BudgetLedger:
        """Create a child whose activity is charged to this ledger as well."""

        return BudgetLedger(scope, limits, parent=self)

    def reserve(
        self,
        amounts: Mapping[str, int],
        *,
        reservation_id: str | None = None,
    ) -> BudgetReservation:
        """Atomically reserve capacity in this scope and all ancestors."""

        requested = _validate_amounts(amounts, label="amounts")
        chain = self._chain()
        with self._locked_chain(chain):
            self._check_capacity(chain, requested)
            for ledger in chain:
                ledger._add(ledger._reserved, requested)
        return BudgetReservation(
            ledger=self,
            reservation_id=reservation_id or uuid4().hex,
            amounts=requested,
        )

    def spend(self, amounts: Mapping[str, int]) -> None:
        """Reserve and immediately reconcile a known, final charge."""

        reservation = self.reserve(amounts)
        reservation.reconcile(amounts)

    def snapshot(self) -> BudgetSnapshot:
        """Return counters for this scope only."""

        with self._lock:
            counters = set(self._limits) | set(self._used) | set(self._reserved)
            remaining = {
                counter: max(
                    0,
                    self._limits[counter]
                    - self._used.get(counter, 0)
                    - self._reserved.get(counter, 0),
                )
                for counter in self._limits
            }
            return BudgetSnapshot(
                scope=self.scope,
                limits=MappingProxyType(dict(self._limits)),
                used=MappingProxyType(
                    {counter: self._used.get(counter, 0) for counter in counters}
                ),
                reserved=MappingProxyType(
                    {counter: self._reserved.get(counter, 0) for counter in counters}
                ),
                remaining=MappingProxyType(remaining),
            )

    def _chain(self) -> tuple[BudgetLedger, ...]:
        chain: list[BudgetLedger] = []
        seen: set[int] = set()
        current: BudgetLedger | None = self
        while current is not None:
            if id(current) in seen:
                raise RuntimeError("budget hierarchy contains a cycle")
            seen.add(id(current))
            chain.append(current)
            current = current.parent
        chain.reverse()
        return tuple(chain)

    @staticmethod
    @contextmanager
    def _locked_chain(chain: tuple[BudgetLedger, ...]) -> Iterator[None]:
        # Always lock root-to-leaf, which prevents deadlocks between siblings.
        for ledger in chain:
            ledger._lock.acquire()
        try:
            yield
        finally:
            for ledger in reversed(chain):
                ledger._lock.release()

    @staticmethod
    def _add(target: dict[str, int], amounts: Mapping[str, int]) -> None:
        for counter, amount in amounts.items():
            target[counter] = target.get(counter, 0) + amount

    @staticmethod
    def _subtract(target: dict[str, int], amounts: Mapping[str, int]) -> None:
        for counter, amount in amounts.items():
            remaining = target.get(counter, 0) - amount
            if remaining < 0:  # This is an invariant failure, not caller input.
                raise RuntimeError(f"negative reserved budget for {counter!r}")
            if remaining:
                target[counter] = remaining
            else:
                target.pop(counter, None)

    @staticmethod
    def _check_capacity(
        chain: tuple[BudgetLedger, ...],
        amounts: Mapping[str, int],
        *,
        replacing: Mapping[str, int] | None = None,
    ) -> None:
        replaced = replacing or {}
        for ledger in chain:
            for counter, amount in amounts.items():
                limit = ledger._limits.get(counter)
                if limit is None:
                    continue
                requested_total = (
                    ledger._used.get(counter, 0)
                    + ledger._reserved.get(counter, 0)
                    - replaced.get(counter, 0)
                    + amount
                )
                if requested_total > limit:
                    raise BudgetExceeded(
                        scope=ledger.scope,
                        counter=counter,
                        limit=limit,
                        requested_total=requested_total,
                    )


class BudgetReservation:
    """A single-use reservation returned by :meth:`BudgetLedger.reserve`."""

    def __init__(
        self,
        *,
        ledger: BudgetLedger,
        reservation_id: str,
        amounts: Mapping[str, int],
    ) -> None:
        if not reservation_id:
            raise ValueError("reservation_id must be non-empty")
        self.ledger = ledger
        self.reservation_id = reservation_id
        self.amounts = MappingProxyType(dict(amounts))
        self._active = True

    @property
    def active(self) -> bool:
        return self._active

    def reconcile(self, actual: Mapping[str, int]) -> None:
        """Replace reserved capacity with actual usage at every hierarchy level.

        Actual usage may exceed the estimate only when all hard caps still allow it.
        A failed reconciliation leaves the reservation active and unchanged.
        """

        charged = _validate_amounts(actual, label="actual")
        chain = self.ledger._chain()
        with self.ledger._locked_chain(chain):
            if not self._active:
                raise RuntimeError("reservation is no longer active")
            self.ledger._check_capacity(chain, charged, replacing=self.amounts)
            for ledger in chain:
                ledger._subtract(ledger._reserved, self.amounts)
                ledger._add(ledger._used, charged)
            self._active = False

    def cancel(self) -> None:
        """Release reserved capacity without charging usage."""

        chain = self.ledger._chain()
        with self.ledger._locked_chain(chain):
            if not self._active:
                raise RuntimeError("reservation is no longer active")
            for ledger in chain:
                ledger._subtract(ledger._reserved, self.amounts)
            self._active = False

    release = cancel
