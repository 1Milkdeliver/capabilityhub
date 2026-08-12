"""Deterministic lexical discovery over active capability revisions."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

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
    total_matches: int = 0
    kind_counts: dict[str, int] = field(default_factory=dict)
    inventory: dict[str, JsonValue] | None = None
    ranking_revision: str = "ranking-v1"
    ranking_digest: str = ""
    index_revision: str = ""


@dataclass(frozen=True, slots=True)
class SearchRankingConfig:
    revision: str = "ranking-v1"
    weights: Mapping[str, int] = field(
        default_factory=lambda: {
            "name": 12,
            "coordinate": 8,
            "alias": 7,
            "tag": 7,
            "operation": 6,
            "summary": 4,
            "kind": 2,
            "exact_name": 20,
            "exact_alias": 18,
        }
    )
    allowed_trust_tiers: frozenset[str] | None = None
    max_cost_microusd: int | None = None
    max_latency_ms: int | None = None

    def __post_init__(self) -> None:
        required = {
            "name",
            "coordinate",
            "alias",
            "tag",
            "operation",
            "summary",
            "kind",
            "exact_name",
            "exact_alias",
        }
        selected = dict(self.weights)
        if not self.revision or set(selected) != required or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in selected.values()
        ):
            raise ValueError("search ranking weights are invalid")
        if self.max_cost_microusd is not None and self.max_cost_microusd < 0:
            raise ValueError("max_cost_microusd must be non-negative")
        if self.max_latency_ms is not None and self.max_latency_ms < 0:
            raise ValueError("max_latency_ms must be non-negative")
        object.__setattr__(self, "weights", MappingProxyType(selected))

    @property
    def digest(self) -> str:
        trust_tiers: list[JsonValue] | None = (
            None
            if self.allowed_trust_tiers is None
            else list(sorted(self.allowed_trust_tiers))
        )
        payload: JsonValue = {
            "allowed_trust_tiers": trust_tiers,
            "max_cost_microusd": self.max_cost_microusd,
            "max_latency_ms": self.max_latency_ms,
            "revision": self.revision,
            "weights": dict(self.weights),
        }
        return "sha256:" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SearchEligibility:
    authorized: bool = True
    available: bool = True
    trusted: bool = True
    within_cost: bool = True
    within_latency: bool = True

    @property
    def eligible(self) -> bool:
        return all(
            (
                self.authorized,
                self.available,
                self.trusted,
                self.within_cost,
                self.within_latency,
            )
        )


class LexicalCapabilitySearch:
    """Ranks active manifests using deterministic, explainable lexical weights."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        references: ReferenceSigner,
        *,
        load_ref_ttl_seconds: int = 300,
        ranking: SearchRankingConfig | None = None,
    ) -> None:
        if load_ref_ttl_seconds <= 0:
            raise ValueError("load_ref_ttl_seconds must be positive")
        self._registry = registry
        self._references = references
        self._ttl = load_ref_ttl_seconds
        self._ranking = ranking or SearchRankingConfig()

    @property
    def ranking_revision(self) -> str:
        return self._ranking.revision

    @property
    def ranking_digest(self) -> str:
        return self._ranking.digest

    @property
    def index_revision(self) -> str:
        material = f"{self._registry.active_digest}\0{self.ranking_digest}".encode()
        return "sha256:" + hashlib.sha256(material).hexdigest()

    def search(
        self,
        query: str,
        *,
        scope: str,
        kinds: Iterable[CapabilityKind | str] | None = None,
        limit: int = 8,
        max_output_tokens: int = 900,
        max_output_bytes: int = 1_000_000,
        max_card_bytes: int = 64_000,
        include_cards: bool = True,
        inventory: dict[str, JsonValue] | None = None,
        allowed_revisions: Collection[str] | None = None,
        eligibility: Callable[[CapabilityManifest], SearchEligibility] | None = None,
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
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (max_output_bytes, max_card_bytes)
        ):
            raise _input("invalid_byte_budget", "Search byte budgets must be positive integers.")

        selected_kinds = _normalize_kinds(kinds)
        query_terms = frozenset(_tokens(query))
        ranked: list[tuple[int, str, CapabilityManifest, tuple[str, ...]]] = []
        for coordinate, revision in sorted(self._registry.activations.items()):
            if allowed_revisions is not None and revision not in allowed_revisions:
                continue
            manifest = self._registry.revision(revision)
            if selected_kinds is not None and manifest.kind not in selected_kinds:
                continue
            decision = _metadata_eligibility(manifest, self._ranking)
            if eligibility is not None:
                decision = _combine_eligibility(
                    decision, _external_eligibility(eligibility, manifest)
                )
            if not decision.eligible:
                continue
            score, reasons = _score(manifest, query_terms, self._ranking)
            if query_terms and score == 0:
                continue
            ranked.append((score, coordinate, manifest, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2].identity.revision))
        kind_counts = {
            kind.value: sum(1 for _, _, manifest, _ in ranked if manifest.kind is kind)
            for kind in CapabilityKind
        }
        total_matches = len(ranked)

        cards: list[SearchCard] = []
        truncated = include_cards and len(ranked) > limit
        selected = ranked if include_cards else ()
        omitted = False
        for index, (_, _, manifest, reasons) in enumerate(selected):
            if len(cards) == limit:
                truncated = True
                break
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
            card_bytes = measure_text(canonical_json(_card_dict(card))).utf8_bytes
            if card_bytes > max_card_bytes:
                omitted = True
                continue
            has_more = index + 1 < len(ranked)
            candidate = _response(
                tuple((*cards, card)),
                truncated=has_more,
                total_matches=total_matches,
                kind_counts=kind_counts,
                inventory=inventory,
                ranking=self._ranking,
                index_revision=self.index_revision,
            )
            if (
                candidate.portable_tokens > max_output_tokens
                or candidate.payload_bytes > max_output_bytes
            ):
                truncated = True
                break
            cards.append(card)
        response = _response(
            tuple(cards),
            truncated=truncated or omitted,
            total_matches=total_matches,
            kind_counts=kind_counts,
            inventory=inventory,
            ranking=self._ranking,
            index_revision=self.index_revision,
        )
        if response.portable_tokens > max_output_tokens:
            raise _budget_too_small(response.portable_tokens, max_output_tokens)
        if response.payload_bytes > max_output_bytes:
            raise _bytes_too_small(response.payload_bytes, max_output_bytes)
        return response


def _score(
    manifest: CapabilityManifest,
    query_terms: frozenset[str],
    config: SearchRankingConfig,
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
    aliases = frozenset(
        token for alias in _aliases(manifest) for token in _tokens(alias)
    )
    fields = (
        ("name", config.weights["name"], name),
        ("coordinate", config.weights["coordinate"], coordinate),
        ("alias", config.weights["alias"], aliases),
        ("tag", config.weights["tag"], tags),
        ("operation", config.weights["operation"], operations),
        ("summary", config.weights["summary"], summary),
        ("kind", config.weights["kind"], kind),
    )
    score = 0
    reasons: list[str] = []
    for label, weight, tokens in fields:
        matches = query_terms & tokens
        if matches:
            score += weight * len(matches)
            reasons.append(label)
    if name and name == query_terms:
        score += config.weights["exact_name"]
        reasons.insert(0, "exact_name")
    if any(frozenset(_tokens(alias)) == query_terms for alias in _aliases(manifest)):
        score += config.weights["exact_alias"]
        reasons.insert(0, "exact_alias")
    return score, tuple(reasons)


def _aliases(manifest: CapabilityManifest) -> tuple[str, ...]:
    raw = manifest.metadata.get("aliases", [])
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _metadata_eligibility(
    manifest: CapabilityManifest, config: SearchRankingConfig
) -> SearchEligibility:
    available = manifest.metadata.get("available", True) is True
    cost, cost_valid = _metadata_counter(manifest, "estimatedCostMicrousd")
    latency, latency_valid = _metadata_counter(manifest, "estimatedLatencyMs")
    trusted = (
        config.allowed_trust_tiers is None
        or manifest.trust_tier in config.allowed_trust_tiers
    )
    within_cost = cost_valid and (
        config.max_cost_microusd is None
        or cost is None
        or cost <= config.max_cost_microusd
    )
    within_latency = latency_valid and (
        config.max_latency_ms is None
        or latency is None
        or latency <= config.max_latency_ms
    )
    return SearchEligibility(
        available=available,
        trusted=trusted,
        within_cost=within_cost,
        within_latency=within_latency,
    )


def _metadata_counter(
    manifest: CapabilityManifest, name: str
) -> tuple[int | None, bool]:
    value = manifest.metadata.get(name)
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, False
    return value, True


def _combine_eligibility(
    first: SearchEligibility, second: SearchEligibility
) -> SearchEligibility:
    if not isinstance(second, SearchEligibility):
        raise _input("invalid_search_eligibility", "Search eligibility returned an invalid value.")
    return SearchEligibility(
        authorized=first.authorized and second.authorized,
        available=first.available and second.available,
        trusted=first.trusted and second.trusted,
        within_cost=first.within_cost and second.within_cost,
        within_latency=first.within_latency and second.within_latency,
    )


def _external_eligibility(
    callback: Callable[[CapabilityManifest], SearchEligibility],
    manifest: CapabilityManifest,
) -> SearchEligibility:
    try:
        return callback(manifest)
    except Exception as error:
        raise CapabilityHubError(
            code="search_eligibility_failed",
            category=ErrorCategory.INTERNAL,
            safe_message="Search eligibility could not be evaluated.",
        ) from error


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


def _response(
    cards: tuple[SearchCard, ...],
    *,
    truncated: bool,
    total_matches: int,
    kind_counts: dict[str, int],
    inventory: dict[str, JsonValue] | None,
    ranking: SearchRankingConfig,
    index_revision: str,
) -> SearchResponse:
    portable_tokens = 0
    payload_bytes = 0
    counts_json: dict[str, JsonValue] = {kind: count for kind, count in kind_counts.items()}
    for _ in range(4):
        payload: dict[str, JsonValue] = {
            "cards": [_card_dict(card) for card in cards],
            "kind_counts": counts_json,
            "payload_bytes": payload_bytes,
            "portable_tokens": portable_tokens,
            "total_matches": total_matches,
            "truncated": truncated,
        }
        if inventory is not None:
            payload["inventory"] = inventory
        measured = measure_text(canonical_json(payload))
        if measured.portable_tokens == portable_tokens and measured.utf8_bytes == payload_bytes:
            break
        portable_tokens = measured.portable_tokens
        payload_bytes = measured.utf8_bytes
    return SearchResponse(
        cards,
        portable_tokens,
        payload_bytes,
        truncated,
        total_matches,
        dict(kind_counts),
        dict(inventory) if inventory is not None else None,
        ranking_revision=ranking.revision,
        ranking_digest=ranking.digest,
        index_revision=index_revision,
    )


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


def _bytes_too_small(required: int, maximum: int) -> CapabilityHubError:
    return CapabilityHubError(
        code="search_output_bytes_too_small",
        category=ErrorCategory.BUDGET,
        safe_message="The search byte budget cannot fit the response envelope.",
        details={"required_bytes": required, "max_output_bytes": maximum},
    )
