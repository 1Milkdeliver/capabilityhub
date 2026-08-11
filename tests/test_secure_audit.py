from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.audit import AuditEvent
from capabilityhub.errors import CapabilityHubError
from capabilityhub.secure_audit import SecureAuditLedger, verify_audit_chain

KEY = b"secure-audit-test-key-32-bytes!"


def _event(sequence: int) -> AuditEvent:
    return AuditEvent(
        event_id=f"evt-{sequence}",
        sequence=sequence,
        task_id="task-1",
        event_type="execute",
        capability_revision="test/tool@1#sha256:abc",
        outcome="success",
        portable_tokens=3,
        payload_bytes=12,
        reason_codes=("policy_allow",),
        metadata={
            "operation": "run",
            "provider": "fixture",
            "arguments": {"secret": "SECRET-ARGUMENT"},
            "credentials": "SECRET-CREDENTIAL",
            "output": "SECRET-OUTPUT",
        },
    )


def _tamper_line(path, index: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[index])
    record["event"]["outcome"] = "failure"
    lines[index] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_secure_audit_appends_verifiable_redacted_chain(tmp_path) -> None:
    ledger = SecureAuditLedger(tmp_path / "current", signing_key=KEY)
    ledger.emit(_event(1))
    ledger.emit(_event(2))

    verification = ledger.verify()
    content = ledger.events_path.read_text(encoding="utf-8")

    assert verification.valid is True
    assert verification.record_count == 2
    assert verification.first_hash is not None
    assert verification.last_hash != verification.first_hash
    assert "SECRET-" not in content
    assert '"operation":"run"' in content


@pytest.mark.parametrize("line_index", [0, 1, 2])
def test_secure_audit_detects_first_middle_or_last_record_tampering(tmp_path, line_index) -> None:
    ledger = SecureAuditLedger(tmp_path / "current", signing_key=KEY)
    for sequence in range(1, 4):
        ledger.emit(_event(sequence))
    _tamper_line(ledger.events_path, line_index)

    verification = verify_audit_chain(ledger.directory, signing_key=KEY)

    assert verification.valid is False
    assert verification.error_code == "audit_chain_hash_mismatch"
    with pytest.raises(CapabilityHubError, match="failed verification"):
        ledger.emit(_event(4))


def test_secure_audit_detects_tail_truncation_against_checkpoint(tmp_path) -> None:
    ledger = SecureAuditLedger(tmp_path / "current", signing_key=KEY)
    for sequence in range(1, 4):
        ledger.emit(_event(sequence))
    lines = ledger.events_path.read_text(encoding="utf-8").splitlines()
    ledger.events_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    verification = verify_audit_chain(ledger.directory, signing_key=KEY)

    assert verification.valid is False
    assert verification.error_code == "audit_chain_truncated"


def test_secure_audit_is_thread_safe_with_contiguous_chain_indices(tmp_path) -> None:
    ledger = SecureAuditLedger(tmp_path / "current", signing_key=KEY)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(lambda sequence: ledger.emit(_event(sequence)), range(1, 41)))

    verification, records = ledger.verified_records()

    assert verification.valid is True
    assert verification.record_count == 40
    assert [record["index"] for record in records] == list(range(1, 41))


def test_corrupt_chain_error_does_not_leak_record_content(tmp_path) -> None:
    ledger = SecureAuditLedger(tmp_path / "current", signing_key=KEY)
    ledger.emit(_event(1))
    ledger.events_path.write_text("SECRET-CANARY", encoding="utf-8")

    with pytest.raises(CapabilityHubError) as caught:
        SecureAuditLedger(ledger.directory, signing_key=KEY)

    assert caught.value.code == "secure_audit_chain_invalid"
    assert "SECRET-CANARY" not in str(caught.value.as_dict())
