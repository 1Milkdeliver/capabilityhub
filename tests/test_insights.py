from capabilityhub.audit import AuditEvent
from capabilityhub.insights import loaded_view, providers_view, routing_view
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    OperationSpec,
    OperationType,
)
from capabilityhub.registry import CapabilityRegistry


def _registry() -> tuple[CapabilityRegistry, str]:
    manifest = CapabilityManifest(
        CapabilityIdentity("demo", "tool", "1", "sha256:" + ("a" * 64)),
        CapabilityKind.CLI,
        "Demo tool",
        "cli-process",
        (OperationSpec("run", OperationType.EXECUTE),),
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    return registry, manifest.identity.revision


def test_loaded_and_provider_views_use_registry_and_redacted_audit() -> None:
    registry, revision = _registry()
    events = (
        AuditEvent("1", 1, "hashed", "load", revision, "failure"),
        AuditEvent("2", 2, "hashed", "load", revision, "success", portable_tokens=12),
        AuditEvent("3", 3, "hashed", "search", None, "success"),
    )

    loaded = loaded_view(registry, events)
    providers = providers_view(registry)

    assert loaded["entries"] == [
        {
            "active": True,
            "kind": "cli",
            "portable_tokens": 12,
            "provider": "cli-process",
            "revision": revision,
            "sequence": 2,
        }
    ]
    assert providers["entries"] == [
        {"active": 1, "discovered": 1, "kinds": ["cli"], "provider": "cli-process"}
    ]


def test_routing_view_keeps_only_compact_explanations() -> None:
    payload = {
        "query": "demo",
        "results": [
            {
                "kind": "skill",
                "match_reason": ["exact_name", "summary"],
                "revision": "demo/tool@1#sha256:x",
                "summary": "body is intentionally excluded",
            }
        ],
        "truncated": True,
    }

    result = routing_view(payload)  # type: ignore[arg-type]

    assert result["model_calls"] == 0
    assert result["entries"][0]["rank"] == 1  # type: ignore[index]
    assert "summary" not in result["entries"][0]  # type: ignore[operator,index]


def test_loaded_view_uses_audit_order_when_process_sequences_restart() -> None:
    registry, revision = _registry()
    events = (
        AuditEvent("old", 99, "first-process", "load", revision, "success"),
        AuditEvent("new", 1, "second-process", "load", revision, "success"),
    )

    result = loaded_view(registry, events)

    assert result["entries"][0]["sequence"] == 1  # type: ignore[index]
