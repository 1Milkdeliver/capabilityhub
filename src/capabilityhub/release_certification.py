"""Fail-closed aggregation of release evidence into a signed manifest digest."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from capabilityhub.metering import canonical_json
from capabilityhub.models import JsonValue

SCHEMA = "capabilityhub.release-evidence.v1"
MANIFEST_SCHEMA = "capabilityhub.release-certification.v1"
SIGNATURE_SCHEMA = "capabilityhub.release-signature.v1"
REQUIRED_EVIDENCE = (
    "adversarial",
    "browser",
    "docs_traceability",
    "full_pytest",
    "matrix_36",
    "model_live",
    "mypy",
    "rag_1m",
    "ruff",
    "sandbox_linux",
    "sandbox_windows",
    "search_10k",
    "supply_bundle",
    "wheel",
)


class ReleaseCertificationError(ValueError):
    """Stable, non-secret release gate failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_type: str
    created_at: datetime
    source_revision: str
    subject_digest: str
    status: str
    skipped: int
    metrics: Mapping[str, JsonValue]
    file_digest: str


@dataclass(frozen=True, slots=True)
class CertificationResult:
    manifest: Mapping[str, JsonValue]
    signature: Mapping[str, JsonValue]


def certify_release(
    paths: Iterable[str | Path],
    *,
    source_revision: str,
    subject_digest: str,
    signing_key: bytes,
    key_id: str,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
) -> CertificationResult:
    """Validate every required artifact and return a deterministic signed summary."""

    selected_now = _utc(now or datetime.now(UTC))
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        raise ValueError("signing_key must contain at least 32 bytes")
    _identifier(source_revision, "source_revision")
    _digest(subject_digest)
    _identifier(key_id, "key_id")
    evidence = tuple(_load(path) for path in paths)
    by_type: dict[str, Evidence] = {}
    for item in evidence:
        if item.evidence_type not in REQUIRED_EVIDENCE:
            raise ReleaseCertificationError("release_evidence_unexpected")
        if item.evidence_type in by_type:
            raise ReleaseCertificationError("release_evidence_duplicate")
        _validate_common(
            item,
            source_revision=source_revision,
            subject_digest=subject_digest,
            now=selected_now,
            max_age=max_age,
        )
        _validate_specific(item)
        by_type[item.evidence_type] = item
    if set(by_type) != set(REQUIRED_EVIDENCE):
        raise ReleaseCertificationError("release_evidence_missing")

    records: list[JsonValue] = [
        {
            "created_at": _timestamp(by_type[name].created_at),
            "evidence_type": name,
            "file_sha256": by_type[name].file_digest,
            "metrics": dict(by_type[name].metrics),
        }
        for name in REQUIRED_EVIDENCE
    ]
    manifest: dict[str, JsonValue] = {
        "certified_at": _timestamp(selected_now),
        "evidence": records,
        "evidence_count": len(records),
        "schema": MANIFEST_SCHEMA,
        "source_revision": source_revision,
        "subject_digest": subject_digest,
    }
    manifest_bytes = canonical_json(manifest).encode("utf-8")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    signature = hmac.new(
        signing_key,
        b"capabilityhub-release-certification-v1\0" + manifest_bytes,
        hashlib.sha256,
    ).hexdigest()
    signature_document: dict[str, JsonValue] = {
        "algorithm": "hmac-sha256",
        "key_id": key_id,
        "manifest_sha256": manifest_digest,
        "schema": SIGNATURE_SCHEMA,
        "signature": signature,
    }
    return CertificationResult(manifest, signature_document)


def verify_certification(
    manifest: Mapping[str, JsonValue],
    signature: Mapping[str, JsonValue],
    *,
    signing_key: bytes,
) -> None:
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ReleaseCertificationError("release_manifest_invalid")
    if signature.get("schema") != SIGNATURE_SCHEMA:
        raise ReleaseCertificationError("release_signature_invalid")
    manifest_bytes = canonical_json(dict(manifest)).encode("utf-8")
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    expected = hmac.new(
        signing_key,
        b"capabilityhub-release-certification-v1\0" + manifest_bytes,
        hashlib.sha256,
    ).hexdigest()
    supplied_digest = signature.get("manifest_sha256")
    supplied_signature = signature.get("signature")
    if (
        not isinstance(supplied_digest, str)
        or not hmac.compare_digest(digest, supplied_digest)
        or not isinstance(supplied_signature, str)
        or not hmac.compare_digest(expected, supplied_signature)
    ):
        raise ReleaseCertificationError("release_signature_invalid")


def evidence_document(
    evidence_type: str,
    *,
    source_revision: str,
    subject_digest: str,
    metrics: Mapping[str, JsonValue],
    created_at: datetime | None = None,
    status: str = "passed",
    skipped: int = 0,
) -> dict[str, JsonValue]:
    return {
        "created_at": _timestamp(_utc(created_at or datetime.now(UTC))),
        "evidence_type": evidence_type,
        "metrics": dict(metrics),
        "schema": SCHEMA,
        "skipped": skipped,
        "source_revision": source_revision,
        "status": status,
        "subject_digest": subject_digest,
    }


def _load(path: str | Path) -> Evidence:
    selected = Path(path)
    try:
        raw = selected.read_bytes()
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise TypeError
        evidence_type = _identifier(value.get("evidence_type"), "evidence_type")
        created_at = _parse_time(value.get("created_at"))
        source_revision = _identifier(value.get("source_revision"), "source_revision")
        subject_digest = _digest(value.get("subject_digest"))
        status = _identifier(value.get("status"), "status")
        skipped = _natural(value.get("skipped"))
        metrics = value.get("metrics")
        if not isinstance(metrics, dict):
            raise TypeError
        return Evidence(
            evidence_type,
            created_at,
            source_revision,
            subject_digest,
            status,
            skipped,
            cast(dict[str, JsonValue], metrics),
            hashlib.sha256(raw).hexdigest(),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ReleaseCertificationError("release_evidence_invalid") from error


def _validate_common(
    evidence: Evidence,
    *,
    source_revision: str,
    subject_digest: str,
    now: datetime,
    max_age: timedelta,
) -> None:
    if evidence.status != "passed":
        raise ReleaseCertificationError("release_evidence_not_passed")
    if evidence.skipped != 0:
        raise ReleaseCertificationError("release_evidence_skipped")
    if evidence.source_revision != source_revision or evidence.subject_digest != subject_digest:
        raise ReleaseCertificationError("release_evidence_source_mismatch")
    if evidence.created_at > now + timedelta(minutes=5):
        raise ReleaseCertificationError("release_evidence_from_future")
    if now - evidence.created_at > max_age:
        raise ReleaseCertificationError("release_evidence_stale")


def _validate_specific(evidence: Evidence) -> None:
    metrics = evidence.metrics
    name = evidence.evidence_type
    checks: dict[str, tuple[tuple[str, object], ...]] = {
        "adversarial": (("passed", True), ("external_provider_cases_min", 1)),
        "browser": (("passed", True), ("assertions_min", 1)),
        "docs_traceability": (("passed", True), ("claims_min", 36)),
        "full_pytest": (("failures", 0), ("passed_min", 1)),
        "matrix_36": (("implemented", 36), ("partial", 0), ("total", 36)),
        "model_live": (
            ("live", True),
            ("provider_reported_tokens", True),
            ("trials_min", 30),
        ),
        "mypy": (("errors", 0),),
        "rag_1m": (("chunks_min", 1_000_000), ("production_index", True)),
        "ruff": (("violations", 0),),
        "sandbox_linux": (("enforced", True), ("platform", "linux")),
        "sandbox_windows": (("enforced", True), ("platform", "windows")),
        "search_10k": (("catalog_min", 10_000), ("top3_correct", True)),
        "supply_bundle": (("verified", True), ("online_freshness", True)),
        "wheel": (("smoke_passed", True), ("wheels_min", 1)),
    }
    for key, expected in checks[name]:
        actual = metrics.get(key)
        if key.endswith("_min"):
            if (
                isinstance(actual, bool)
                or not isinstance(actual, int)
                or actual < cast(int, expected)
            ):
                raise ReleaseCertificationError("release_evidence_metric_failed")
        elif actual != expected:
            raise ReleaseCertificationError("release_evidence_metric_failed")


def _write_json(path: str | Path, value: Mapping[str, JsonValue]) -> None:
    selected = Path(path)
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(canonical_json(dict(value)) + "\n", encoding="utf-8")


def _parse_metrics(values: Sequence[str]) -> dict[str, JsonValue]:
    metrics: dict[str, JsonValue] = {}
    for value in values:
        key, separator, raw = value.partition("=")
        if not separator or not key:
            raise ValueError("metrics must use key=JSON format")
        metrics[key] = cast(JsonValue, json.loads(raw))
    return metrics


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("--type", required=True)
    record.add_argument("--source-revision", required=True)
    record.add_argument("--subject-digest", required=True)
    record.add_argument("--metric", action="append", default=[])
    record.add_argument("--status", default="passed")
    record.add_argument("--skipped", type=int, default=0)
    record.add_argument("--output", required=True)
    certify = commands.add_parser("certify")
    certify.add_argument("--evidence-root", required=True)
    certify.add_argument("--source-revision", required=True)
    certify.add_argument("--subject-digest", required=True)
    certify.add_argument("--signing-key-env", default="CAPABILITYHUB_RELEASE_SIGNING_KEY")
    certify.add_argument("--key-id", required=True)
    certify.add_argument("--max-age-hours", type=float, default=24.0)
    certify.add_argument("--manifest", required=True)
    certify.add_argument("--signature", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "record":
        document = evidence_document(
            args.type,
            source_revision=args.source_revision,
            subject_digest=args.subject_digest,
            metrics=_parse_metrics(args.metric),
            status=args.status,
            skipped=args.skipped,
        )
        _write_json(args.output, document)
        return 0
    raw_key = os.environ.get(args.signing_key_env)
    if raw_key is None:
        raise ReleaseCertificationError("release_signing_key_missing")
    paths = sorted(Path(args.evidence_root).rglob("evidence-*.json"))
    result = certify_release(
        paths,
        source_revision=args.source_revision,
        subject_digest=args.subject_digest,
        signing_key=raw_key.encode("utf-8"),
        key_id=args.key_id,
        max_age=timedelta(hours=args.max_age_hours),
    )
    _write_json(args.manifest, result.manifest)
    _write_json(args.signature, result.signature)
    return 0


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise TypeError(f"{label} is invalid")
    return value


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TypeError("subject_digest is invalid")
    return value


def _natural(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("value must be a natural number")
    return value


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TypeError("timestamp is invalid")
    return _utc(datetime.fromisoformat(value[:-1] + "+00:00"))


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseCertificationError as error:
        print(error.code, file=sys.stderr)
        raise SystemExit(1) from None
