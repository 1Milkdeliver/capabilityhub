"""Read-only local text retrieval provider with bounded citations."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time
from typing import cast

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json, measure_text
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.rag_index import DiskRagIndex, RagHit
from capabilityhub.tenancy import TenantScope

_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class LocalRagFixture:
    manifest: CapabilityManifest
    root: Path
    operation: str = "retrieve"
    suffixes: tuple[str, ...] = (".md", ".txt")
    chunk_lines: int = 12
    max_files: int = 500
    max_file_bytes: int = 512_000
    index: DiskRagIndex | None = None

    def __post_init__(self) -> None:
        root = self.root.resolve()
        if not root.is_dir():
            raise ValueError("RAG root must be an existing directory")
        object.__setattr__(self, "root", root)
        if self.manifest.operation(self.operation) is None:
            raise ValueError("RAG operation must be declared by the manifest")
        if not self.suffixes or any(not item.startswith(".") for item in self.suffixes):
            raise ValueError("RAG suffixes must be non-empty file extensions")
        object.__setattr__(self, "suffixes", tuple(item.casefold() for item in self.suffixes))
        if self.chunk_lines < 1 or self.max_files < 1 or self.max_file_bytes < 1:
            raise ValueError("RAG scan bounds must be positive")


class LocalRagProvider:
    """Search approved local text roots without indexing or exposing whole files."""

    def __init__(
        self,
        fixtures: tuple[LocalRagFixture, ...] | list[LocalRagFixture],
        *,
        name: str = "local-rag",
    ) -> None:
        if not name:
            raise ValueError("RAG provider name must not be empty")
        self._name = name
        values = tuple(fixtures)
        revisions = [fixture.manifest.identity.revision for fixture in values]
        if len(revisions) != len(set(revisions)):
            raise ValueError("RAG fixture revisions must be unique")
        if any(fixture.manifest.provider != name for fixture in values):
            raise ValueError("RAG manifest provider must match the configured provider name")
        self._fixtures = values
        self._by_revision = {fixture.manifest.identity.revision: fixture for fixture in values}

    @property
    def name(self) -> str:
        return self._name

    def discover(self) -> tuple[CapabilityManifest, ...]:
        return tuple(fixture.manifest for fixture in self._fixtures)

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        fixture = self._by_revision.get(identity.revision)
        if fixture is None:
            raise _error(
                "rag_capability_not_found",
                ErrorCategory.REFERENCE,
                "The requested RAG source is not configured.",
            )
        if request.operation != fixture.operation:
            raise _error(
                "rag_operation_not_found",
                ErrorCategory.REFERENCE,
                "The requested RAG operation is not allowlisted.",
            )
        query = request.arguments.get("query")
        top_k = request.arguments.get("top_k", 5)
        expansion_handle = request.arguments.get("expansion_handle")
        if expansion_handle is not None and not isinstance(expansion_handle, str):
            raise _error(
                "rag_expansion_handle_invalid",
                ErrorCategory.INPUT,
                "RAG expansion handle must be a string.",
            )
        if expansion_handle is None and (not isinstance(query, str) or not query.strip()):
            raise _error("rag_query_invalid", ErrorCategory.INPUT, "RAG query must be non-empty.")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
            raise _error(
                "rag_top_k_invalid",
                ErrorCategory.INPUT,
                "RAG top_k must be an integer from 1 to 20.",
            )
        max_bytes = request.arguments.get("max_bytes", 64_000)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
            raise _error(
                "rag_max_bytes_invalid", ErrorCategory.INPUT, "RAG max_bytes must be positive."
            )
        filters = request.arguments.get("filters", {})
        if not isinstance(filters, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in filters.items()
        ):
            raise _error("rag_filters_invalid", ErrorCategory.INPUT, "RAG filters are invalid.")
        scope = TenantScope(
            context.tenant_id, context.principal_id, context.session_id, request.task_id
        )
        results: list[JsonValue]
        truncated = False
        if fixture.index is not None:
            hits: tuple[RagHit, ...]
            if expansion_handle is not None:
                hits = (
                    fixture.index.expand(
                        expansion_handle, scope=scope, max_bytes=max_bytes, now=time()
                    ),
                )
            else:
                assert isinstance(query, str)
                hits = fixture.index.search(
                    query,
                    scope=scope,
                    top_k=top_k,
                    filters=cast(dict[str, str], filters),
                    max_bytes=max_bytes,
                    now=time(),
                )
            results = [_indexed_result(hit) for hit in hits]
        else:
            assert isinstance(query, str)
            terms = tuple(dict.fromkeys(word.casefold() for word in _WORD.findall(query)))
            candidates = _candidates(fixture, terms, monotonic() + context.deadline_ms / 1000)
            results = []
            used_bytes = 0
            for score, relative, start_line, text in candidates[:top_k]:
                text_bytes = len(text.encode("utf-8"))
                if used_bytes + text_bytes > max_bytes:
                    truncated = True
                    break
                used_bytes += text_bytes
                results.append(
                    {
                        "citation": {
                            "end_line": start_line + text.count("\n"),
                            "path": relative,
                            "start_line": start_line,
                        },
                        "score": score,
                        "text": text,
                    }
                )
            truncated = truncated or len(candidates) > len(results)
        output: JsonValue = {"results": results, "truncated": truncated}
        while results and len(canonical_json(output).encode("utf-8")) > max_bytes:
            results.pop()
            output = {"results": results, "truncated": True}
        if len(canonical_json(output).encode("utf-8")) > max_bytes:
            raise _error(
                "rag_response_bytes_exceeded",
                ErrorCategory.BUDGET,
                "The RAG response cannot fit the hard byte budget.",
            )
        measurement = measure_text(canonical_json(output))
        while results and measurement.portable_tokens > context.max_output_tokens:
            results.pop()
            output = {"results": results, "truncated": True}
            measurement = measure_text(canonical_json(output))
        if measurement.portable_tokens > context.max_output_tokens:
            raise _error(
                "rag_output_budget_exceeded",
                ErrorCategory.BUDGET,
                "The RAG response cannot fit the hard output budget.",
            )
        audit_material = canonical_json(
            {
                "operation": request.operation,
                "query_digest": hashlib.sha256(
                    (query if isinstance(query, str) else "expansion").encode()
                ).hexdigest(),
                "result_count": len(results),
                "revision": identity.revision,
                "task_id": request.task_id,
            }
        ).encode()
        return ExecutionResult(
            capability_revision=identity.revision,
            operation=request.operation,
            output=output,
            provider=self.name,
            portable_tokens=measurement.portable_tokens,
            audit_id=f"rag-{hashlib.sha256(audit_material).hexdigest()[:16]}",
        )


def _candidates(
    fixture: LocalRagFixture, terms: tuple[str, ...], deadline: float
) -> list[tuple[int, str, int, str]]:
    candidates: list[tuple[int, str, int, str]] = []
    visited = 0
    for path in sorted(fixture.root.rglob("*"), key=lambda item: item.as_posix()):
        if monotonic() > deadline:
            raise _error(
                "rag_deadline_exceeded",
                ErrorCategory.TIMEOUT,
                "The RAG retrieval exceeded its deadline.",
            )
        if visited >= fixture.max_files:
            break
        if not path.is_file() or path.suffix.casefold() not in fixture.suffixes:
            continue
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(fixture.root).as_posix()
        except (OSError, ValueError):
            continue
        visited += 1
        try:
            with resolved.open("rb") as stream:
                raw = stream.read(fixture.max_file_bytes + 1)
        except OSError:
            continue
        if len(raw) > fixture.max_file_bytes:
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        for offset in range(0, len(lines), fixture.chunk_lines):
            if monotonic() > deadline:
                raise _error(
                    "rag_deadline_exceeded",
                    ErrorCategory.TIMEOUT,
                    "The RAG retrieval exceeded its deadline.",
                )
            text = "\n".join(lines[offset : offset + fixture.chunk_lines]).strip()
            folded = text.casefold()
            score = sum(folded.count(term) for term in terms)
            if text and score:
                candidates.append((score, relative, offset + 1, text))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    return candidates


def _indexed_result(hit: RagHit) -> JsonValue:
    return {
        "citation": hit.citation,
        "created_at": hit.created_at,
        "expansion_handle": hit.expansion_handle,
        "fresh": hit.fresh,
        "fresh_until": hit.fresh_until,
        "passage_id": hit.chunk_id,
        "retain_until": hit.retain_until,
        "score": hit.score,
        "text": hit.text,
    }


def _error(code: str, category: ErrorCategory, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=category, safe_message=message)
