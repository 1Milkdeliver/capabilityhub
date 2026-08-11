"""Deterministic lexical discovery over active capability revisions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json, measure_text
from capabilityhub.models import (
    CapabilityKind,
    CapabilityManifest,
    JsonValue,
    SearchCard,
    maximum_side_effect,
)
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry

_WORD = re.compile(r"[^\W_]+(?:[_-][^\W_]+)*", re.UNICODE)


CapabilitySearchCard = SearchCard


@dataclass(frozen=True, slots=True)
class SearchResponse:
    cards: tuple[SearchCard, ...]
    portable_tokens: int
    payload_bytes: int
    truncated: bool = False


class LexicalCapabilitySearch:
    """Ranks active manifests using deterministic, explainable lexical weights."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        references: ReferenceSigner,
        *,
        load_ref_ttl_seconds: int = 300,
    ) -> None:
        if load_ref_ttl_seconds <= 0:
            raise ValueError("load_ref_ttl_seconds must be positive")
        self._registry = registry
        self._references = references
        self._ttl = load_ref_ttl_seconds

    def search(
        self,
        query: str,
        *,
        scope: str,
        kinds: Iterable[CapabilityKind | str] | None = None,
        limit: int = 8,
        max_output_tokens: int = 900,
    ) -> SearchResponse:
        if not isinstance(query, str):
            raise _input("invalid_search_query", "Search query must be text.")
        if not scope:
            raise _input("invalid_search_scope", "Search scope must be non-empty.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise _input("invalid_search_limit", "Search limit must be a positive integer.")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 1
        ):
            raise _input(
                "invalid_output_budget", "Search output budget must be a positive integer."
            )

        selected_kinds = _normalize_kinds(kinds)
        query_terms = frozenset(_tokens(query))
        ranked: list[tuple[int, str, CapabilityManifest, tuple[str, ...]]] = []
        for coordinate, revision in sorted(self._registry.activations.items()):
            manifest = self._registry.revision(revision)
            if selected_kinds is not None and manifest.kind not in selected_kinds:
                continue
            score, reasons = _score(manifest, query_terms)
            if query_terms and score == 0:
                continue
            ranked.append((score, coordinate, manifest, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2].identity.revision))

        cards: list[SearchCard] = []
        truncated = len(ranked) > limit
        for index, (_, _, manifest, reasons) in enumerate(ranked[:limit]):
            capability_ref = self._references.issue(
                revision=manifest.identity.revision,
                scope=scope,
                purpose="load",
                ttl_seconds=self._ttl,
            )
            card = SearchCard(
                revision=manifest.identity.revision,
                kind=manifest.kind,
                summary=manifest.summary,
                operations=tuple(operation.name for operation in manifest.operations),
                risk=maximum_side_effect(manifest.operations),
                trust_tier=manifest.trust_tier,
                estimated_load_tokens=sum(section.portable_tokens for section in manifest.sections),
                match_reason=reasons or ("active_catalog",),
                capability_ref=capability_ref,
            )
            has_more = index + 1 < min(len(ranked), limit) or len(ranked) > limit
            candidate = _response(tuple((*cards, card)), truncated=has_more)
            if candidate.portable_tokens > max_output_tokens:
                truncated = True
                break
            cards.append(card)
        response = _response(tuple(cards), truncated=truncated)
        if response.portable_tokens > max_output_tokens:
            raise _budget_too_small(response.portable_tokens, max_output_tokens)
        return response


def _score(
    manifest: CapabilityManifest, query_terms: frozenset[str]
) -> tuple[int, tuple[str, ...]]:
    if not query_terms:
        return 0, ("active_catalog",)

    coordinate = frozenset(_tokens(manifest.identity.coordinate))
    name = frozenset(_tokens(manifest.identity.name))
    summary = frozenset(_tokens(manifest.summary))
    tags = frozenset(token for tag in manifest.tags for token in _tokens(tag))
    operations = frozenset(
        token for operation in manifest.operations for token in _tokens(operation.name)
    )
    kind = frozenset((manifest.kind.value,))
    fields = (
        ("name", 12, name),
        ("coordinate", 8, coordinate),
        ("tag", 7, tags),
        ("operation", 6, operations),
        ("summary", 4, summary),
        ("kind", 2, kind),
    )
    score = 0
    reasons: list[str] = []
    for label, weight, tokens in fields:
        matches = query_terms & tokens
        if matches:
            score += weight * len(matches)
            reasons.append(label)
    if name and name == query_terms:
        score += 20
        reasons.insert(0, "exact_name")
    return score, tuple(reasons)


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _WORD.finditer(value))


def _normalize_kinds(
    kinds: Iterable[CapabilityKind | str] | None,
) -> frozenset[CapabilityKind] | None:
    if kinds is None:
        return None
    if isinstance(kinds, (CapabilityKind, str)):
        kinds = (kinds,)
    try:
        return frozenset(CapabilityKind(kind) for kind in kinds)
    except ValueError as error:
        raise _input("invalid_capability_kind", "Unknown capability kind.") from error


def _response(cards: tuple[SearchCard, ...], *, truncated: bool) -> SearchResponse:
    portable_tokens = 0
    payload_bytes = 0
    for _ in range(4):
        payload: dict[str, JsonValue] = {
            "cards": [_card_dict(card) for card in cards],
            "payload_bytes": payload_bytes,
            "portable_tokens": portable_tokens,
            "truncated": truncated,
        }
        measured = measure_text(canonical_json(payload))
        if measured.portable_tokens == portable_tokens and measured.utf8_bytes == payload_bytes:
            break
        portable_tokens = measured.portable_tokens
        payload_bytes = measured.utf8_bytes
    return SearchResponse(cards, portable_tokens, payload_bytes, truncated)


def _card_dict(card: SearchCard) -> dict[str, JsonValue]:
    return {
        "capability_ref": card.capability_ref,
        "estimated_load_tokens": card.estimated_load_tokens,
        "kind": card.kind.value,
        "match_reason": list(card.match_reason),
        "operations": list(card.operations),
        "revision": card.revision,
        "risk": card.risk.value,
        "summary": card.summary,
        "trust_tier": card.trust_tier,
    }


def _input(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INPUT, safe_message=message)


def _budget_too_small(required: int, maximum: int) -> CapabilityHubError:
    return CapabilityHubError(
        code="search_output_budget_too_small",
        category=ErrorCategory.BUDGET,
        safe_message="The search output budget cannot fit the response envelope.",
        details={"required_tokens": required, "max_output_tokens": maximum},
    )
