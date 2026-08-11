from __future__ import annotations

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
from capabilityhub.search import CapabilitySearchCard, LexicalCapabilitySearch


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
