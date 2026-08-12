from __future__ import annotations

from dataclasses import replace

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
    SectionDescriptor,
)
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.search import (
    CapabilitySearchCard,
    LexicalCapabilitySearch,
    SearchEligibility,
    SearchRankingConfig,
)


def _manifest(
    name: str,
    summary: str,
    *,
    kind: CapabilityKind = CapabilityKind.API,
    tags: tuple[str, ...] = (),
) -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", name, "1.0.0", f"digest-{name}"),
        kind=kind,
        summary=summary,
        provider="fixture",
        operations=(OperationSpec("find", OperationType.EXECUTE),),
        sections=(SectionDescriptor("guide", "text/plain", "guide", 2),),
        tags=tags,
    )


def _search(*manifests: CapabilityManifest) -> tuple[LexicalCapabilitySearch, ReferenceSigner]:
    registry = CapabilityRegistry()
    registry.register_many(manifests)
    for manifest in manifests:
        registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    signer = ReferenceSigner(b"search-test-key", clock=lambda: 100)
    return LexicalCapabilitySearch(registry, signer), signer


def test_search_is_deterministic_and_returns_revision_bound_load_refs() -> None:
    search, signer = _search(
        _manifest("generic", "Search ordinary records."),
        _manifest("issues", "Search payment failure issues.", tags=("payments",)),
    )

    response = search.search("payment issues", scope="scope", max_output_tokens=2_000)
    assert response.cards[0].revision.startswith("test/issues@")
    assert isinstance(response.cards[0], CapabilitySearchCard)
    claims = signer.verify(
        response.cards[0].capability_ref,
        expected_scope="scope",
        expected_purpose="load",
        expected_revision=response.cards[0].revision,
    )
    assert claims.revision == response.cards[0].revision


def test_search_reads_only_active_revisions_and_filters_kind() -> None:
    api = _manifest("api-item", "shared term", kind=CapabilityKind.API)
    rag = _manifest("rag-item", "shared term", kind=CapabilityKind.RAG)
    inactive = _manifest("inactive", "shared term", kind=CapabilityKind.RAG)
    registry = CapabilityRegistry()
    registry.register_many((api, rag, inactive))
    registry.activate(api.identity.coordinate, api.identity.revision)
    registry.activate(rag.identity.coordinate, rag.identity.revision)
    search = LexicalCapabilitySearch(registry, ReferenceSigner(b"search-key", clock=lambda: 100))

    response = search.search(
        "shared", scope="scope", kinds=(CapabilityKind.RAG,), max_output_tokens=2_000
    )
    assert [card.revision for card in response.cards] == [rag.identity.revision]
    assert response.total_matches == 1
    assert response.kind_counts == {"skill": 0, "mcp": 0, "cli": 0, "api": 0, "rag": 1}


def test_search_hard_budget_truncates_without_exceeding_the_limit() -> None:
    search, _ = _search(
        _manifest("a", "term " * 40),
        _manifest("b", "term " * 40),
        _manifest("c", "term " * 40),
    )
    envelope = search.search("missing", scope="scope", max_output_tokens=100)
    budget = envelope.portable_tokens + 140
    response = search.search("term", scope="scope", max_output_tokens=budget)

    assert response.portable_tokens <= budget
    assert response.truncated
    assert len(response.cards) < 3


def test_search_rejects_a_budget_too_small_for_its_envelope() -> None:
    search, _ = _search(_manifest("item", "term"))
    with pytest.raises(CapabilityHubError) as raised:
        search.search("term", scope="scope", max_output_tokens=1)
    assert raised.value.code == "search_output_budget_too_small"


def test_counts_only_inventory_is_global_bounded_and_has_no_references() -> None:
    search, _ = _search(
        _manifest("api", "shared", kind=CapabilityKind.API),
        _manifest("skill", "shared", kind=CapabilityKind.SKILL),
    )
    inventory = {
        "generation": 4,
        "active_total": 2,
        "active_by_kind": {"skill": 1, "mcp": 0, "cli": 0, "api": 1, "rag": 0},
        "status": "fresh",
    }

    response = search.search(
        "shared",
        scope="scope",
        kinds=(CapabilityKind.SKILL,),
        include_cards=False,
        inventory=inventory,
        max_output_tokens=2_000,
    )

    assert response.cards == ()
    assert not response.truncated
    assert response.total_matches == 1
    assert response.kind_counts["skill"] == 1
    assert response.inventory == inventory
    assert response.inventory["active_total"] == 2
    assert response.portable_tokens < 2_000


def test_total_and_per_card_byte_limits_skip_malicious_oversized_card() -> None:
    oversized = _manifest("needle", "needle " * 20_000)
    safe = _manifest("safe", "needle", tags=("needle",))
    search, _ = _search(oversized, safe)

    response = search.search(
        "needle",
        scope="scope",
        max_output_tokens=10_000,
        max_output_bytes=2_000,
        max_card_bytes=800,
    )

    assert [card.revision for card in response.cards] == [safe.identity.revision]
    assert response.payload_bytes <= 2_000
    assert response.truncated is True


def test_ranking_alias_weights_and_revision_change_are_deterministic() -> None:
    alias = replace(_manifest("alias-item", "ordinary"), metadata={"aliases": ["needle"]})
    summary = _manifest("summary-item", "needle")
    registry = CapabilityRegistry()
    registry.register_many((alias, summary))
    for manifest in (alias, summary):
        registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    default = SearchRankingConfig()
    weights = {**default.weights, "alias": 100, "exact_alias": 200}
    configured = SearchRankingConfig(revision="ranking-v2", weights=weights)
    search = LexicalCapabilitySearch(
        registry,
        ReferenceSigner(b"ranking-test-key", clock=lambda: 100),
        ranking=configured,
    )

    first = search.search("needle", scope="scope", max_output_tokens=2_000)
    second = search.search("needle", scope="scope", max_output_tokens=2_000)

    assert first.cards[0].revision == alias.identity.revision
    assert first.cards[0].match_reason[:2] == ("exact_alias", "alias")
    assert [card.revision for card in first.cards] == [card.revision for card in second.cards]
    assert first.ranking_revision == "ranking-v2"
    assert first.ranking_digest == configured.digest != default.digest
    assert first.index_revision == second.index_revision


def test_all_eligibility_dimensions_filter_before_ranking_without_endpoint_leak() -> None:
    base = _manifest("eligible", "needle")
    unavailable = replace(_manifest("unavailable", "needle"), metadata={"available": False})
    costly = replace(
        _manifest("costly", "needle"), metadata={"estimatedCostMicrousd": 101}
    )
    slow = replace(_manifest("slow", "needle"), metadata={"estimatedLatencyMs": 51})
    untrusted = replace(_manifest("untrusted", "needle"), trust_tier="unverified")
    trusted = replace(base, trust_tier="verified")
    search, _ = _search(unavailable, costly, slow, untrusted, trusted)
    ranking = SearchRankingConfig(
        allowed_trust_tiers=frozenset({"verified"}),
        max_cost_microusd=100,
        max_latency_ms=50,
    )
    filtered = LexicalCapabilitySearch(
        search._registry,
        ReferenceSigner(b"eligibility-test-key", clock=lambda: 100),
        ranking=ranking,
    )

    response = filtered.search(
        "needle",
        scope="scope",
        max_output_tokens=2_000,
        allowed_revisions={trusted.identity.revision},
        eligibility=lambda manifest: SearchEligibility(
            authorized=manifest.identity.revision == trusted.identity.revision
        ),
    )

    assert [card.revision for card in response.cards] == [trusted.identity.revision]
    assert "endpoint" not in str(response.cards[0].match_reason).casefold()
