"""Tamper-evident append-only storage for payload-minimized audit events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from capabilityhub.audit import AuditEvent
from capabilityhub.errors import CapabilityHubError, ErrorCategory

AUDIT_SCHEMA = "capabilityhub.secure-audit"
AUDIT_VERSION = 1
AUDIT_ALGORITHM = "hmac-sha256"
GENESIS_HASH = "0" * 64
_MAX_RECORD_BYTES = 1_048_576
_MAX_CHECKPOINT_BYTES = 65_536
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:@/#-]+$")
_SAFE_METADATA = frozenset(
    {
        "operation",
        "operation_count",
        "provider",
        "result_count",
        "section_count",
        "skill_load_only",
        "total_matches",
        "truncated",
    }
)


@dataclass(frozen=True, slots=True)
class ChainVerification:
    schema: str
    version: int
    algorithm: str
    valid: bool
    record_count: int
    first_hash: str | None
    last_hash: str
    error_code: str | None = None


class SecureAuditLedger:
    """Append allowlisted AuditEvent fields to one HMAC-chained local segment."""

    def __init__(self, directory: str | Path, *, signing_key: bytes) -> None:
        if len(signing_key) < 16:
            raise ValueError("signing_key must contain at least 16 bytes")
        self._directory = Path(directory).resolve()
        self._events_path = self._directory / "events.jsonl"
        self._checkpoint_path = self._directory / "checkpoint.json"
        self._key = bytes(signing_key)
        self._lock = RLock()
        verification = self.verify()
        if not verification.valid:
            raise _invalid_chain(verification.error_code)

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def checkpoint_path(self) -> Path:
        return self._checkpoint_path

    def emit(self, event: AuditEvent) -> None:
        safe_event = _safe_event(event)
        with self._lock:
            verification, _ = _read_verified(self._directory, self._key)
            if not verification.valid:
                raise _invalid_chain(verification.error_code)
            index = verification.record_count + 1
            unsigned: dict[str, Any] = {
                "algorithm": AUDIT_ALGORITHM,
                "event": safe_event,
                "index": index,
                "previous_hash": verification.last_hash,
                "schema": AUDIT_SCHEMA,
                "version": AUDIT_VERSION,
            }
            chain_hash = _mac(self._key, unsigned)
            record = {**unsigned, "chain_hash": chain_hash}
            encoded = (_canonical(record) + "\n").encode("utf-8")
            if len(encoded) > _MAX_RECORD_BYTES:
                raise _audit_error(
                    "audit_event_too_large",
                    "The audit event exceeds the secure record limit.",
                )
            self._directory.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(
                    self._events_path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                try:
                    written = os.write(descriptor, encoded)
                    if written != len(encoded):
                        raise OSError("short append")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                _write_checkpoint(
                    self._checkpoint_path,
                    self._key,
                    record_count=index,
                    last_hash=chain_hash,
                )
            except OSError as error:
                raise _audit_error(
                    "secure_audit_write_failed",
                    "The secure audit record could not be committed.",
                ) from error

    def verify(self) -> ChainVerification:
        with self._lock:
            verification, _ = _read_verified(self._directory, self._key)
            return verification

    def verified_records(self) -> tuple[ChainVerification, tuple[dict[str, Any], ...]]:
        """Return verified records for retention/export without exposing the key."""

        with self._lock:
            verification, records = _read_verified(self._directory, self._key)
            if not verification.valid:
                raise _invalid_chain(verification.error_code)
            return verification, records

    def rotate_to(self, destination: str | Path) -> ChainVerification:
        """Atomically move a verified non-empty segment and reset this ledger."""

        target = Path(destination).resolve()
        with self._lock:
            verification, _ = _read_verified(self._directory, self._key)
            if not verification.valid:
                raise _invalid_chain(verification.error_code)
            if verification.record_count == 0:
                raise _audit_error("secure_audit_empty", "An empty audit segment cannot rotate.")
            if target.exists():
                raise _audit_error(
                    "secure_audit_rotation_conflict",
                    "The audit archive destination already exists.",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(self._directory, target)
            except OSError as error:
                raise _audit_error(
                    "secure_audit_rotation_failed",
                    "The secure audit segment could not be rotated.",
                ) from error
            return verification


def verify_audit_chain(directory: str | Path, *, signing_key: bytes) -> ChainVerification:
    if len(signing_key) < 16:
        raise ValueError("signing_key must contain at least 16 bytes")
    verification, _ = _read_verified(Path(directory).resolve(), bytes(signing_key))
    return verification


def read_verified_segment(
    directory: str | Path, *, signing_key: bytes
) -> tuple[ChainVerification, tuple[dict[str, Any], ...]]:
    if len(signing_key) < 16:
        raise ValueError("signing_key must contain at least 16 bytes")
    verification, records = _read_verified(Path(directory).resolve(), bytes(signing_key))
    if not verification.valid:
        raise _invalid_chain(verification.error_code)
    return verification, records


def _read_verified(
    directory: Path, key: bytes
) -> tuple[ChainVerification, tuple[dict[str, Any], ...]]:
    events_path = directory / "events.jsonl"
    checkpoint_path = directory / "checkpoint.json"
    events_exists = events_path.is_file()
    checkpoint_exists = checkpoint_path.is_file()
    if not events_exists and not checkpoint_exists:
        return _verification(True, 0, None, GENESIS_HASH), ()
    if not events_exists or not checkpoint_exists:
        return _verification(False, 0, None, GENESIS_HASH, "audit_chain_incomplete"), ()
    records: list[dict[str, Any]] = []
    previous_hash = GENESIS_HASH
    first_hash: str | None = None
    try:
        with events_path.open("rb") as stream:
            while line := stream.readline(_MAX_RECORD_BYTES + 1):
                if len(line) > _MAX_RECORD_BYTES or not line.endswith(b"\n"):
                    return _invalid_result(
                        records, first_hash, previous_hash, "audit_record_invalid"
                    )
                record = json.loads(line)
                if not isinstance(record, dict):
                    return _invalid_result(
                        records, first_hash, previous_hash, "audit_record_invalid"
                    )
                error_code = _record_error(record, len(records) + 1, previous_hash, key)
                if error_code is not None:
                    return _invalid_result(records, first_hash, previous_hash, error_code)
                chain_hash = record["chain_hash"]
                assert isinstance(chain_hash, str)
                first_hash = first_hash or chain_hash
                previous_hash = chain_hash
                records.append(record)
        raw_checkpoint = checkpoint_path.read_bytes()
        if len(raw_checkpoint) > _MAX_CHECKPOINT_BYTES:
            raise ValueError("checkpoint too large")
        checkpoint = json.loads(raw_checkpoint)
        if not _valid_checkpoint(checkpoint, key):
            return _invalid_result(records, first_hash, previous_hash, "audit_checkpoint_invalid")
        if checkpoint["record_count"] != len(records) or checkpoint["last_hash"] != previous_hash:
            return _invalid_result(records, first_hash, previous_hash, "audit_chain_truncated")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return _invalid_result(records, first_hash, previous_hash, "audit_chain_unreadable")
    return _verification(True, len(records), first_hash, previous_hash), tuple(records)


def _record_error(record: dict[str, Any], index: int, previous: str, key: bytes) -> str | None:
    if (
        record.get("schema") != AUDIT_SCHEMA
        or record.get("version") != AUDIT_VERSION
        or record.get("algorithm") != AUDIT_ALGORITHM
        or record.get("index") != index
        or record.get("previous_hash") != previous
        or not isinstance(record.get("event"), dict)
        or not isinstance(record.get("chain_hash"), str)
    ):
        return "audit_record_invalid"
    unsigned = {name: value for name, value in record.items() if name != "chain_hash"}
    if not hmac.compare_digest(record["chain_hash"], _mac(key, unsigned)):
        return "audit_chain_hash_mismatch"
    return None


def _valid_checkpoint(value: object, key: bytes) -> bool:
    if not isinstance(value, dict):
        return False
    unsigned = {name: item for name, item in value.items() if name != "checkpoint_mac"}
    return (
        value.get("schema") == AUDIT_SCHEMA
        and value.get("version") == AUDIT_VERSION
        and value.get("algorithm") == AUDIT_ALGORITHM
        and isinstance(value.get("record_count"), int)
        and not isinstance(value.get("record_count"), bool)
        and value["record_count"] >= 0
        and isinstance(value.get("last_hash"), str)
        and isinstance(value.get("checkpoint_mac"), str)
        and hmac.compare_digest(value["checkpoint_mac"], _mac(key, unsigned))
    )


def _write_checkpoint(path: Path, key: bytes, *, record_count: int, last_hash: str) -> None:
    unsigned = {
        "algorithm": AUDIT_ALGORITHM,
        "last_hash": last_hash,
        "record_count": record_count,
        "schema": AUDIT_SCHEMA,
        "version": AUDIT_VERSION,
    }
    payload = {**unsigned, "checkpoint_mac": _mac(key, unsigned)}
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(_canonical(payload))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _safe_event(event: AuditEvent) -> dict[str, Any]:
    identifiers = (event.event_id, event.task_id, event.event_type, event.outcome)
    if any(not _safe_identifier(value, 256) for value in identifiers):
        raise _audit_error("audit_event_invalid", "The audit event contains an invalid field.")
    counters = (event.sequence, event.portable_tokens, event.payload_bytes)
    if any(not _natural(value) for value in counters):
        raise _audit_error("audit_event_invalid", "The audit event contains an invalid field.")
    if event.capability_revision is not None and not _safe_identifier(
        event.capability_revision, 512
    ):
        raise _audit_error("audit_event_invalid", "The audit event contains an invalid field.")
    if any(not _safe_identifier(code, 128) for code in event.reason_codes):
        raise _audit_error("audit_event_invalid", "The audit event contains an invalid field.")
    metadata: dict[str, bool | int | str] = {}
    for name, value in (event.metadata or {}).items():
        if name not in _SAFE_METADATA or isinstance(value, (dict, list)) or value is None:
            continue
        if isinstance(value, str):
            if len(value) <= 256:
                metadata[name] = value
        elif isinstance(value, (bool, int)):
            metadata[name] = value
    return {
        "capability_revision": event.capability_revision,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "metadata": metadata,
        "outcome": event.outcome,
        "payload_bytes": event.payload_bytes,
        "portable_tokens": event.portable_tokens,
        "reason_codes": list(event.reason_codes),
        "sequence": event.sequence,
        "task_id": event.task_id,
    }


def _safe_identifier(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and _SAFE_IDENTIFIER.fullmatch(value) is not None
    )


def _natural(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _mac(key: bytes, value: Mapping[str, Any]) -> str:
    return hmac.new(key, _canonical(value).encode("utf-8"), hashlib.sha256).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _invalid_result(
    records: list[dict[str, Any]],
    first_hash: str | None,
    last_hash: str,
    error_code: str,
) -> tuple[ChainVerification, tuple[dict[str, Any], ...]]:
    return _verification(False, len(records), first_hash, last_hash, error_code), ()


def _verification(
    valid: bool,
    count: int,
    first_hash: str | None,
    last_hash: str,
    error_code: str | None = None,
) -> ChainVerification:
    return ChainVerification(
        schema=AUDIT_SCHEMA,
        version=AUDIT_VERSION,
        algorithm=AUDIT_ALGORITHM,
        valid=valid,
        record_count=count,
        first_hash=first_hash,
        last_hash=last_hash,
        error_code=error_code,
    )


def _invalid_chain(reason: str | None) -> CapabilityHubError:
    return CapabilityHubError(
        code="secure_audit_chain_invalid",
        category=ErrorCategory.INTERNAL,
        safe_message="The secure audit chain failed verification.",
        details={"reason": reason or "unknown"},
    )


def _audit_error(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INTERNAL, safe_message=message)
