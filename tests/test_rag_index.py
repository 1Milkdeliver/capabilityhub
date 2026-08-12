from __future__ import annotations

import sqlite3

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.rag_index import DiskRagIndex, RagPassage
from capabilityhub.tenancy import TenantScope

KEY = b"production-rag-index-test-key"


def _scope(tenant: str, principal: str, task: str = "task") -> TenantScope:
    return TenantScope(tenant, principal, "session", task)


def test_index_isolates_tenants_filters_deduplicates_and_bounds_bytes(tmp_path) -> None:
    index = DiskRagIndex(tmp_path / "rag.sqlite3", scope_key=KEY)
    alpha = _scope("alpha", "alice")
    beta = _scope("beta", "alice")
    assert index.replace(
        alpha,
        [
            RagPassage("one", "a#1", "unique needle", {"locale": "en"}),
            RagPassage("duplicate", "a#2", "unique needle", {"locale": "en"}),
            RagPassage("zh", "a#3", "needle 中文", {"locale": "zh"}),
        ],
    ) == 2
    index.replace(beta, [RagPassage("secret", "b#1", "SECRET-CANARY needle")])

    hits = index.search("needle", scope=alpha, filters={"locale": "en"}, max_bytes=20)
    assert [hit.chunk_id for hit in hits] == ["one"]
    assert "SECRET-CANARY" not in str(hits)
    assert index.search("needle", scope=_scope("gamma", "alice")) == ()


def test_acl_updates_apply_to_search_and_expansion_without_reindex(tmp_path) -> None:
    index = DiskRagIndex(tmp_path / "rag.sqlite3", scope_key=KEY)
    alice = _scope("tenant", "alice")
    bob = _scope("tenant", "bob", "different-task")
    index.replace(
        alice,
        [RagPassage("private", "docs#1", "private needle", allowed_principals=("alice",))],
    )
    selected = index.search("needle", scope=alice)[0]
    before = sqlite3.connect(index.path).execute("SELECT count(*) FROM rag_fts").fetchone()[0]
    assert index.search("needle", scope=bob) == ()

    index.set_acl(alice, "private", ("bob",))

    assert index.search("needle", scope=alice) == ()
    assert index.search("needle", scope=bob)[0].chunk_id == "private"
    assert index.expand(selected.expansion_handle, scope=bob).text == "private needle"
    with pytest.raises(CapabilityHubError) as denied:
        index.expand(selected.expansion_handle, scope=alice)
    assert denied.value.code == "rag_passage_unavailable"
    after = sqlite3.connect(index.path).execute("SELECT count(*) FROM rag_fts").fetchone()[0]
    assert after == before


def test_freshness_retention_and_handle_fail_closed_without_existence_leak(tmp_path) -> None:
    index = DiskRagIndex(tmp_path / "rag.sqlite3", scope_key=KEY)
    scope = _scope("tenant", "alice")
    index.replace(
        scope,
        [
            RagPassage(
                "old",
                "docs#2",
                "archive needle",
                created_at=10,
                fresh_until=20,
                retain_until=40,
            )
        ],
    )
    stale = index.search("needle", scope=scope, now=30)[0]
    assert stale.fresh is False
    assert stale.created_at == 10
    assert stale.fresh_until == 20
    assert stale.retain_until == 40
    assert index.search("needle", scope=scope, now=40) == ()
    assert index.purge_expired(_scope("other", "alice"), now=40) == 0
    assert index.purge_expired(scope, now=40) == 1
    for handle in (stale.expansion_handle, "rag1.missing.invalid"):
        with pytest.raises(CapabilityHubError) as unavailable:
            index.expand(handle, scope=_scope("other", "alice"), now=30)
        assert unavailable.value.code == "rag_passage_unavailable"
        assert unavailable.value.details == {}
