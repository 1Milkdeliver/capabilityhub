from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from capabilityhub.models import ReasoningTier
from capabilityhub.reasoning_store import SQLiteReasoningStore, StoredReasoningState


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
