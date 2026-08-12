from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from capabilityhub.models import ReasoningTier
from capabilityhub.reasoning_store import (
    ScopedReasoningStore,
    SQLiteReasoningStore,
    StoredReasoningState,
)
from capabilityhub.tenancy import SqliteScopedState, TenantScope

_SCOPE_KEY = b"reasoning-tenant-scope-test-key"


def test_store_round_trips_only_digests_and_compact_recommendation(tmp_path) -> None:
    path = tmp_path / "reasoning.sqlite3"
    store = SQLiteReasoningStore(path)

    def update(state: StoredReasoningState) -> tuple[StoredReasoningState, None]:
        return (
            StoredReasoningState(
                task_id=state.task_id,
                tier=ReasoningTier.MEDIUM,
                escalations_used=1,
                recommendation_count=2,
                attempt_counts={("attempt-sha256", "evidence-sha256"): 2},
                last_recommendation={"tier": "medium", "should_stop": True},
            ),
            None,
        )

    store.transact("task", update)
    restored = SQLiteReasoningStore(path).read("task")

    assert restored.tier is ReasoningTier.MEDIUM
    assert restored.escalations_used == 1
    assert restored.attempt_counts == {("attempt-sha256", "evidence-sha256"): 2}
    assert restored.last_recommendation == {"tier": "medium", "should_stop": True}
    assert b"raw secret prompt" not in path.read_bytes()


def test_store_serializes_concurrent_task_updates(tmp_path) -> None:
    store = SQLiteReasoningStore(tmp_path / "reasoning.sqlite3")

    def increment(_: int) -> None:
        def update(state: StoredReasoningState) -> tuple[StoredReasoningState, None]:
            return (
                StoredReasoningState(
                    task_id=state.task_id,
                    tier=state.tier,
                    escalations_used=state.escalations_used,
                    recommendation_count=state.recommendation_count + 1,
                    attempt_counts=state.attempt_counts,
                    last_recommendation=state.last_recommendation,
                ),
                None,
            )

        store.transact("shared", update)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment, range(40)))

    assert store.read("shared").recommendation_count == 40


def test_reset_removes_task_and_attempt_rows(tmp_path) -> None:
    store = SQLiteReasoningStore(tmp_path / "reasoning.sqlite3")

    def update(state: StoredReasoningState) -> tuple[StoredReasoningState, None]:
        return (
            StoredReasoningState(
                task_id=state.task_id,
                tier=ReasoningTier.LOW,
                attempt_counts={("a", "e"): 1},
            ),
            None,
        )

    store.transact("task", update)
    store.reset("task")

    assert store.read("task") == StoredReasoningState(task_id="task")


def test_scoped_store_isolates_same_task_concurrently_and_restores(tmp_path) -> None:
    path = tmp_path / "reasoning.sqlite3"

    def store_for(tenant: str) -> ScopedReasoningStore:
        return ScopedReasoningStore(
            SqliteScopedState(path, scope_key=_SCOPE_KEY),
            scope_provider=lambda task: TenantScope(
                tenant, "same-principal", "same-session", task
            ),
        )

    stores = (store_for("TENANT-CANARY-A"), store_for("TENANT-CANARY-B"))

    def increment(store: ScopedReasoningStore) -> None:
        def update(state: StoredReasoningState) -> tuple[StoredReasoningState, None]:
            return (
                StoredReasoningState(
                    state.task_id,
                    tier=ReasoningTier.LOW,
                    recommendation_count=state.recommendation_count + 1,
                ),
                None,
            )

        store.transact("same-task", update)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(increment, (stores[index % 2] for index in range(40))))

    assert store_for("TENANT-CANARY-A").read("same-task").recommendation_count == 20
    assert store_for("TENANT-CANARY-B").read("same-task").recommendation_count == 20
    assert store_for("TENANT-CANARY-C").read("same-task").recommendation_count == 0
    raw = path.read_bytes()
    assert b"TENANT-CANARY" not in raw
    assert b"same-principal" not in raw
    assert b"same-session" not in raw
    assert b"same-task" not in raw
