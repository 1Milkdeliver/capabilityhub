from pathlib import Path

from capabilityhub.audit import AuditEvent, JsonlAuditSink, MemoryAuditSink, read_jsonl_audit
from capabilityhub.metering import Utf8Div4Estimator, canonical_json, measure_text
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
    SideEffect,
)
from capabilityhub.policy import PolicyContext, PolicyOutcome, ReferencePolicy


def _manifest(operation: OperationSpec, permissions: tuple[str, ...] = ()) -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity("test", "item", "1.0.0", "sha256:abc"),
        kind=CapabilityKind.API,
        summary="test capability",
        provider="static",
        operations=(operation,),
        permissions=permissions,
    )


def test_metering_is_deterministic_and_labeled_as_estimate() -> None:
    assert canonical_json({"b": 1, "a": "值"}) == '{"a":"值","b":1}'
    measured = measure_text("abcd值", Utf8Div4Estimator())
    assert measured.utf8_bytes == 7
    assert measured.portable_tokens == 2
    assert measured.estimator == "utf8-bytes-div-4-v1"


def test_policy_denies_missing_permission_before_approval() -> None:
    operation = OperationSpec(
        "update", OperationType.EXECUTE, side_effect=SideEffect.REVERSIBLE_WRITE
    )
    decision = ReferencePolicy().decide(
        _manifest(operation, ("issues.write",)),
        operation,
        PolicyContext(frozenset()),
    )
    assert decision.outcome is PolicyOutcome.DENY
    assert decision.reason_codes[0] == "missing_permission"


def test_policy_requires_approval_for_write() -> None:
    operation = OperationSpec(
        "update", OperationType.EXECUTE, side_effect=SideEffect.REVERSIBLE_WRITE
    )
    policy = ReferencePolicy()
    assert (
        policy.decide(_manifest(operation), operation, PolicyContext(frozenset())).outcome
        is PolicyOutcome.APPROVAL_REQUIRED
    )
    assert (
        policy.decide(
            _manifest(operation), operation, PolicyContext(frozenset(), approved=True)
        ).outcome
        is PolicyOutcome.ALLOW
    )


def test_audit_sinks_store_structured_events(tmp_path: Path) -> None:
    event = AuditEvent("evt-1", 1, "task-1", "search", None, "success", portable_tokens=3)
    memory = MemoryAuditSink()
    memory.emit(event)
    assert memory.events == [event]

    output = tmp_path / "audit.jsonl"
    JsonlAuditSink(output).emit(event)
    text = output.read_text(encoding="utf-8")
    assert '"event_type":"search"' in text
    assert text.endswith("\n")

    output.write_text("invalid partial\n" + text, encoding="utf-8")
    assert read_jsonl_audit(output, limit=1) == (event,)
