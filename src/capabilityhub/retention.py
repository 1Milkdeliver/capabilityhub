"""Atomic rotation, bounded retention, and verified export for secure audit ledgers."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.secure_audit import (
    ChainVerification,
    SecureAuditLedger,
    read_verified_segment,
)

EXPORT_SCHEMA = "capabilityhub.secure-audit-export"
EXPORT_VERSION = 1
_SEGMENT = re.compile(r"^segment-(\d{20})$")


@dataclass(frozen=True, slots=True)
class RotationResult:
    archive: Path
    verification: ChainVerification
    retained_segments: int
    removed_segments: int


class AuditRetentionManager:
    """Own a current ledger and retain a bounded set of immutable segments."""

    def __init__(
        self,
        root: str | Path,
        *,
        signing_key: bytes,
        max_segments: int = 10,
    ) -> None:
        if not 1 <= max_segments <= 1_000:
            raise ValueError("max_segments must be from 1 to 1000")
        self._root = Path(root).resolve()
        self._archive_root = self._root / "archive"
        self._key = bytes(signing_key)
        self._max_segments = max_segments
        self._lock = RLock()
        self.ledger = SecureAuditLedger(self._root / "current", signing_key=self._key)

    @property
    def archive_root(self) -> Path:
        return self._archive_root

    @property
    def archives(self) -> tuple[Path, ...]:
        with self._lock:
            return tuple(self._segments())

    def rotate(self) -> RotationResult:
        with self._lock:
            segments = self._segments()
            next_index = _segment_index(segments[-1]) + 1 if segments else 1
            target = self._archive_root / f"segment-{next_index:020d}"
            verification = self.ledger.rotate_to(target)
            removed = self._prune()
            return RotationResult(
                archive=target,
                verification=verification,
                retained_segments=len(self._segments()),
                removed_segments=removed,
            )

    def export_jsonl(self, source: str | Path, destination: str | Path) -> Path:
        """Verify and atomically export one current or archived segment."""

        selected = Path(source).resolve()
        target = Path(destination).resolve()
        with self._lock:
            if target.is_relative_to(self._root):
                raise _retention_error(
                    "audit_export_destination_denied",
                    "The audit export destination must be outside the managed ledger.",
                )
            if selected == self.ledger.directory:
                verification, records = self.ledger.verified_records()
            elif selected.parent == self._archive_root and _SEGMENT.fullmatch(selected.name):
                verification, records = read_verified_segment(selected, signing_key=self._key)
            else:
                raise _retention_error(
                    "audit_export_source_denied",
                    "The audit export source is outside the managed ledger.",
                )
            summary: dict[str, Any] = {
                "chain_verification": asdict(verification),
                "export_schema": EXPORT_SCHEMA,
                "export_version": EXPORT_VERSION,
                "record_type": "verification_summary",
            }
            _write_export(target, summary, records)
            return target

    def _segments(self) -> list[Path]:
        try:
            entries = tuple(self._archive_root.iterdir())
        except FileNotFoundError:
            return []
        except OSError as error:
            raise _retention_error(
                "audit_retention_unreadable",
                "The secure audit archive could not be inspected.",
            ) from error
        return sorted(
            (path for path in entries if path.is_dir() and _SEGMENT.fullmatch(path.name)),
            key=_segment_index,
        )

    def _prune(self) -> int:
        segments = self._segments()
        expired = segments[: max(0, len(segments) - self._max_segments)]
        for path in expired:
            try:
                shutil.rmtree(path)
            except OSError as error:
                raise _retention_error(
                    "audit_retention_failed",
                    "An expired secure audit segment could not be removed.",
                ) from error
        return len(expired)


def _write_export(
    target: Path,
    summary: dict[str, Any],
    records: tuple[dict[str, Any], ...],
) -> None:
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_canonical(summary))
            stream.write("\n")
            for record in records:
                stream.write(_canonical(record))
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as error:
        raise _retention_error(
            "audit_export_failed",
            "The verified audit export could not be written.",
        ) from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _segment_index(path: Path) -> int:
    match = _SEGMENT.fullmatch(path.name)
    if match is None:
        raise ValueError("invalid secure audit segment name")
    return int(match.group(1))


def _retention_error(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INTERNAL, safe_message=message)
