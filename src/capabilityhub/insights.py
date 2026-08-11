"""Compact, no-model views for Loaded, Providers, and Routing menus."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from capabilityhub.audit import AuditEvent
from capabilityhub.models import JsonValue
from capabilityhub.registry import CapabilityRegistry


def loaded_view(
    registry: CapabilityRegistry,
    events: Iterable[AuditEvent],
    *,
    limit: int = 20,
) -> dict[str, JsonValue]:
    """Return the latest successful loads, derived from redacted durable audit."""

    if not 1 <= limit <= 100:
        raise ValueError("loaded limit must be from 1 to 100")
    latest: dict[str, tuple[int, AuditEvent]] = {}
    for position, event in enumerate(events):
        if event.event_type == "load" and event.outcome == "success" and event.capability_revision:
            latest[event.capability_revision] = (position, event)
    active = frozenset(registry.activations.values())
    rows: list[JsonValue] = []
    ordered = sorted(latest.values(), key=lambda item: item[0], reverse=True)[:limit]
    for _, event in ordered:
        revision = event.capability_revision
        assert revision is not None
        manifest = registry.revisions.get(revision)
        rows.append(
            {
                "active": revision in active,
                "kind": manifest.kind.value if manifest is not None else "unavailable",
                "portable_tokens": event.portable_tokens,
                "provider": manifest.provider if manifest is not None else "unavailable",
                "revision": revision,
                "sequence": event.sequence,
            }
        )
    return {
        "entries": rows,
        "meaning": "recent_successful_loads",
        "source": "redacted_project_audit",
        "stored": len(rows),
    }


def providers_view(registry: CapabilityRegistry) -> dict[str, JsonValue]:
    """Group discovered and active revisions by their explicit Provider name."""

    active = frozenset(registry.activations.values())
    discovered: dict[str, int] = {}
    active_counts: dict[str, int] = {}
    kinds: dict[str, set[str]] = {}
    for revision, manifest in sorted(registry.revisions.items()):
        provider = manifest.provider
        discovered[provider] = discovered.get(provider, 0) + 1
        active_counts[provider] = active_counts.get(provider, 0) + int(revision in active)
        kinds.setdefault(provider, set()).add(manifest.kind.value)
    entries: list[JsonValue] = [
        {
            "active": active_counts[name],
            "discovered": discovered[name],
            "kinds": cast(list[JsonValue], sorted(kinds[name])),
            "provider": name,
        }
        for name in sorted(discovered)
    ]
    return {"entries": entries, "provider_count": len(entries), "scope": "local_catalog"}


def routing_view(search_payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Explain deterministic lexical routing without invoking a model."""

    source = search_payload.get("results")
    results = source if isinstance(source, list) else []
    entries: list[JsonValue] = []
    for rank, raw in enumerate(results, start=1):
        if not isinstance(raw, dict):
            continue
        entries.append(
            {
                "kind": raw.get("kind"),
                "match_reason": raw.get("match_reason", []),
                "rank": rank,
                "revision": raw.get("revision"),
            }
        )
    return {
        "entries": entries,
        "method": "deterministic_lexical",
        "model_calls": 0,
        "query": search_payload.get("query", ""),
        "truncated": search_payload.get("truncated", False),
    }
