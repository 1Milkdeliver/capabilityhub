from __future__ import annotations

from collections.abc import Mapping

import pytest

from capabilityhub.admin_control import (
    AdminPrincipal,
    AdminRequestEnvelope,
    AuthenticatedAdminDispatcher,
)
from capabilityhub.audit import MemoryAuditSink
from capabilityhub.auth import AuthIdentity
from capabilityhub.errors import CapabilityHubError
from capabilityhub.models import JsonValue


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, JsonValue], AuthIdentity]] = []

    def dispatch(
        self,
        operation: str,
        payload: Mapping[str, JsonValue],
        identity: AuthIdentity,
    ) -> JsonValue:
        self.calls.append((operation, payload, identity))
        return {"operation": operation, "source": identity.source}


@pytest.mark.parametrize("source", ("admin-loopback", "admin-cli", "admin-dashboard"))
def test_all_admin_entries_share_role_identity_and_envelope(source: str) -> None:
    backend = _Backend()
    audit = MemoryAuditSink()
    identity = AuthIdentity("tenant", "operator", source, "session")
    dispatcher = AuthenticatedAdminDispatcher(
        backend,
        AdminPrincipal(identity, frozenset(("lifecycle-operator",))),
        audit=audit,
    )

    result = dispatcher.dispatch(
        AdminRequestEnvelope("request", "lifecycle.list", {})
    )

    assert result == {"operation": "lifecycle.list", "source": source}
    assert backend.calls == [("lifecycle.list", {}, identity)]
    assert audit.events[0].reason_codes == (f"admin_authenticated:{source}",)


def test_dispatcher_denies_wrong_role_before_backend() -> None:
    backend = _Backend()
    identity = AuthIdentity("tenant", "operator", "admin-cli", "session")
    dispatcher = AuthenticatedAdminDispatcher(
        backend,
        AdminPrincipal(identity, frozenset(("auditor",))),
        audit=MemoryAuditSink(),
    )

    with pytest.raises(CapabilityHubError) as denied:
        dispatcher.dispatch(AdminRequestEnvelope("request", "approval.decide", {}))

    assert denied.value.code == "admin_role_denied"
    assert backend.calls == []
