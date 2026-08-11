from __future__ import annotations

import json

import pytest

import capabilityhub.secure_audit as secure_audit_module
from capabilityhub.audit import AuditEvent
from capabilityhub.errors import CapabilityHubError
from capabilityhub.retention import EXPORT_SCHEMA, EXPORT_VERSION, AuditRetentionManager
from capabilityhub.secure_audit import verify_audit_chain

KEY = b"secure-retention-test-key-32bytes"


def _event(sequence: int) -> AuditEvent:
    return AuditEvent(
        event_id=f"evt-{sequence}",
        sequence=sequence,
        task_id="task-1",
        event_type="load",
        capability_revision="test/tool@1#sha256:abc",
        outcome="success",
        metadata={"operation": "load", "output": "SECRET-OUTPUT"},
    )


def test_rotation_moves_whole_verified_segment_and_bounds_retention(tmp_path) -> None:
    manager = AuditRetentionManager(tmp_path / "audit", signing_key=KEY, max_segments=2)
    rotated = []
    for sequence in range(1, 4):
        manager.ledger.emit(_event(sequence))
        rotated.append(manager.rotate())

    assert [item.removed_segments for item in rotated] == [0, 0, 1]
    assert [path.name for path in manager.archives] == [
        "segment-00000000000000000002",
        "segment-00000000000000000003",
    ]
    assert not rotated[0].archive.exists()
    for archive in manager.archives:
        assert verify_audit_chain(archive, signing_key=KEY).valid is True
    assert manager.ledger.verify().record_count == 0


def test_failed_atomic_rotation_keeps_current_segment_intact(tmp_path, monkeypatch) -> None:
    manager = AuditRetentionManager(tmp_path / "audit", signing_key=KEY)
    manager.ledger.emit(_event(1))

    def fail_replace(source, destination) -> None:
        del source, destination
        raise OSError("SECRET-CANARY")

    monkeypatch.setattr(secure_audit_module.os, "replace", fail_replace)
    with pytest.raises(CapabilityHubError) as caught:
        manager.rotate()

    assert caught.value.code == "secure_audit_rotation_failed"
    assert "SECRET-CANARY" not in str(caught.value.as_dict())
    assert manager.ledger.verify().record_count == 1
    assert manager.archives == ()


def test_export_is_jsonl_with_chain_summary_and_no_sensitive_payload(tmp_path) -> None:
    manager = AuditRetentionManager(tmp_path / "audit", signing_key=KEY)
    manager.ledger.emit(_event(1))
    archive = manager.rotate().archive
    destination = tmp_path / "exports" / "audit.jsonl"

    manager.export_jsonl(archive, destination)
    lines = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]

    assert lines[0]["export_schema"] == EXPORT_SCHEMA
    assert lines[0]["export_version"] == EXPORT_VERSION
    assert lines[0]["chain_verification"]["valid"] is True
    assert lines[0]["chain_verification"]["record_count"] == 1
    assert lines[1]["schema"] == "capabilityhub.secure-audit"
    assert "SECRET-OUTPUT" not in destination.read_text(encoding="utf-8")


def test_export_refuses_tampered_source_and_does_not_publish_file(tmp_path) -> None:
    manager = AuditRetentionManager(tmp_path / "audit", signing_key=KEY)
    manager.ledger.emit(_event(1))
    archive = manager.rotate().archive
    events = archive / "events.jsonl"
    events.write_text("SECRET-CANARY", encoding="utf-8")
    destination = tmp_path / "export.jsonl"

    with pytest.raises(CapabilityHubError) as caught:
        manager.export_jsonl(archive, destination)

    assert caught.value.code == "secure_audit_chain_invalid"
    assert "SECRET-CANARY" not in str(caught.value.as_dict())
    assert not destination.exists()


def test_export_cannot_overwrite_managed_chain_files(tmp_path) -> None:
    manager = AuditRetentionManager(tmp_path / "audit", signing_key=KEY)
    manager.ledger.emit(_event(1))

    with pytest.raises(CapabilityHubError) as caught:
        manager.export_jsonl(manager.ledger.directory, manager.ledger.events_path)

    assert caught.value.code == "audit_export_destination_denied"
    assert manager.ledger.verify().valid is True
