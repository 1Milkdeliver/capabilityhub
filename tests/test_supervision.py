from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from capabilityhub.audit import MemoryAuditSink
from capabilityhub.budget import BudgetLedger
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    OperationSpec,
    OperationType,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.providers.http import (
    EnvironmentHeaders,
    HttpApiFixture,
    HttpApiProvider,
    HttpInvocation,
)
from capabilityhub.providers.static import StaticFixture, StaticProvider
from capabilityhub.references import ReferenceSigner
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.service import CapabilityHubService, ServiceContext
from capabilityhub.supervision import (
    ProcessProviderSupervisor,
    WorkerResourceLimits,
    sandbox_capabilities,
)

IDENTITY = CapabilityIdentity("test", "worker", "1", "sha256:" + "0" * 64)
REQUEST = ExecutionRequest("execution-ref", "run", {"value": 1}, "task")


class _Provider:
    name = "worker-fixture"

    def discover(self):
        return ()

    def execute(self, identity, request, context):
        del context
        return ExecutionResult(
            identity.revision,
            request.operation,
            {"ok": True},
            self.name,
            3,
            "worker-audit",
        )


class _SlowProvider(_Provider):
    def execute(self, identity, request, context):
        del identity, request, context
        time.sleep(5)
        raise AssertionError("unreachable")


class _CrashProvider(_Provider):
    def execute(self, identity, request, context):
        del identity, request, context
        os._exit(7)


class _UnsafeFailureProvider(_Provider):
    def execute(self, identity, request, context):
        del identity, request, context
        raise RuntimeError("SECRET-CANARY")


class _SafeFailureProvider(_Provider):
    def execute(self, identity, request, context):
        del identity, request, context
        raise CapabilityHubError(
            code="upstream_unavailable",
            category=ErrorCategory.PROVIDER,
            safe_message="The configured upstream is unavailable.",
            retryable=True,
            details={"unsafe": "SECRET-CANARY"},
        )


class _UnserializableProvider(_Provider):
    def __init__(self) -> None:
        self.callback = lambda: None


class _HugeProvider(_Provider):
    def execute(self, identity, request, context):
        del context
        return ExecutionResult(
            identity.revision,
            request.operation,
            {"content": "x" * 20_000},
            self.name,
            3,
            "worker-audit",
        )


class _ChildProvider(_Provider):
    def __init__(self, pid_path: str) -> None:
        self.pid_path = pid_path

    def execute(self, identity, request, context):
        del identity, request, context
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,time,sys; "
                "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)",
                self.pid_path,
            ]
        )
        child.wait()
        raise AssertionError("unreachable")


def _context(deadline_ms: int = 5_000) -> ProviderContext:
    return ProviderContext("tenant", "principal", "session", deadline_ms, 1_000)


def test_process_supervisor_returns_json_result_from_spawned_worker() -> None:
    result = ProcessProviderSupervisor().execute(_Provider(), IDENTITY, REQUEST, _context())

    assert result.output == {"ok": True}
    assert result.capability_revision == IDENTITY.revision
    assert result.provider == "worker-fixture"


def test_process_supervisor_terminates_worker_after_deadline() -> None:
    with pytest.raises(CapabilityHubError) as caught:
        ProcessProviderSupervisor().execute(_SlowProvider(), IDENTITY, REQUEST, _context(150))

    assert caught.value.code == "provider_worker_timeout"
    assert caught.value.category is ErrorCategory.TIMEOUT
    assert caught.value.retryable is True


def test_process_supervisor_cancels_registered_worker_tree() -> None:
    supervisor = ProcessProviderSupervisor()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(supervisor.execute, _SlowProvider(), IDENTITY, REQUEST, _context())
        deadline = time.monotonic() + 3
        while supervisor.active_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

        assert supervisor.cancel(REQUEST.execution_ref) is True
        with pytest.raises(CapabilityHubError) as caught:
            future.result(timeout=3)

    assert caught.value.code == "provider_worker_cancelled"
    assert caught.value.retryable is False
    assert supervisor.active_count() == 0


def test_unsupported_worker_isolation_fails_closed() -> None:
    with pytest.raises(ValueError, match="network isolation"):
        WorkerResourceLimits(require_network_isolation=True)

    capabilities = sandbox_capabilities()
    assert capabilities.filesystem_isolation is None
    assert capabilities.network_isolation is None
    assert capabilities.cpu_limit in {"job-object", "setrlimit"}


def test_cpu_and_memory_limits_are_enforced_by_platform_backend() -> None:
    supervisor = ProcessProviderSupervisor(
        resource_limits=WorkerResourceLimits(cpu_seconds=2, memory_bytes=128 * 1024 * 1024)
    )
    result = supervisor.execute(_Provider(), IDENTITY, REQUEST, _context())
    assert result.output == {"ok": True}


def test_cancellation_terminates_descendant_process(tmp_path) -> None:
    pid_path = tmp_path / "child.pid"
    supervisor = ProcessProviderSupervisor()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            supervisor.execute,
            _ChildProvider(str(pid_path)),
            IDENTITY,
            REQUEST,
            _context(10_000),
        )
        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.exists()
        child_pid = int(pid_path.read_text())
        assert supervisor.cancel(REQUEST.execution_ref)
        with pytest.raises(CapabilityHubError):
            future.result(timeout=5)

    deadline = time.monotonic() + 3
    while _pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(child_pid)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        output = subprocess.run(
            ("tasklist.exe", "/FI", f"PID eq {pid}", "/NH"),
            check=False,
            capture_output=True,
            text=True,
        ).stdout
        return str(pid) in output
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_process_supervisor_classifies_hard_worker_crash() -> None:
    with pytest.raises(CapabilityHubError) as caught:
        ProcessProviderSupervisor().execute(_CrashProvider(), IDENTITY, REQUEST, _context())

    assert caught.value.code == "provider_worker_crashed"
    assert caught.value.category is ErrorCategory.PROVIDER


def test_process_supervisor_redacts_unhandled_worker_exception() -> None:
    with pytest.raises(CapabilityHubError) as caught:
        ProcessProviderSupervisor().execute(_UnsafeFailureProvider(), IDENTITY, REQUEST, _context())

    assert caught.value.code == "provider_worker_failed"
    assert "SECRET-CANARY" not in str(caught.value.as_dict())


def test_process_supervisor_preserves_only_safe_structured_error_fields() -> None:
    with pytest.raises(CapabilityHubError) as caught:
        ProcessProviderSupervisor().execute(_SafeFailureProvider(), IDENTITY, REQUEST, _context())

    assert caught.value.code == "upstream_unavailable"
    assert caught.value.retryable is True
    assert caught.value.details == {}
    assert "SECRET-CANARY" not in str(caught.value.as_dict())


def test_process_supervisor_rejects_unserializable_provider_safely() -> None:
    with pytest.raises(CapabilityHubError) as caught:
        ProcessProviderSupervisor().execute(
            _UnserializableProvider(), IDENTITY, REQUEST, _context()
        )

    assert caught.value.code == "provider_worker_not_serializable"
    assert "lambda" not in str(caught.value.as_dict())


def test_process_supervisor_rejects_oversized_result_without_decoding_it() -> None:
    with pytest.raises(CapabilityHubError) as caught:
        ProcessProviderSupervisor(max_message_bytes=1_024).execute(
            _HugeProvider(), IDENTITY, REQUEST, _context()
        )

    assert caught.value.code == "provider_worker_result_too_large"
    assert caught.value.category is ErrorCategory.BUDGET


def test_strict_local_policy_rejects_unknown_provider_instead_of_fake_isolation() -> None:
    with pytest.raises(CapabilityHubError) as caught:
        ProcessProviderSupervisor(strict_local_providers=True).execute(
            _Provider(), IDENTITY, REQUEST, _context()
        )

    assert caught.value.code == "provider_worker_type_unsupported"
    assert caught.value.category is ErrorCategory.POLICY


def test_brokered_http_missing_alias_fails_before_worker_spawn() -> None:
    manifest = CapabilityManifest(
        identity=IDENTITY,
        kind=CapabilityKind.API,
        summary="Secret boundary fixture",
        provider="http-api",
        operations=(OperationSpec("run", OperationType.EXECUTE),),
    )
    provider = HttpApiProvider(
        (
            HttpApiFixture(
                manifest,
                "http://127.0.0.1:9",
                {"run": HttpInvocation("GET", "/")},
                headers=EnvironmentHeaders((("Authorization", "PRIVATE_ALIAS"),)),
            ),
        )
    )

    with pytest.raises(CapabilityHubError) as caught:
        ProcessProviderSupervisor(strict_local_providers=True).execute(
            provider,
            IDENTITY,
            REQUEST,
            _context(),
        )

    expected = (
        "secret_alias_unavailable"
        if os.name == "nt"
        else "provider_worker_secret_boundary_unsupported"
    )
    assert caught.value.code == expected
    assert "PRIVATE_ALIAS" not in repr(caught.value.as_dict())


def test_service_can_execute_static_provider_through_process_supervisor() -> None:
    manifest = CapabilityManifest(
        identity=IDENTITY,
        kind=CapabilityKind.API,
        summary="Worker supervised fixture",
        provider="static-worker",
        operations=(OperationSpec("run", OperationType.EXECUTE),),
    )
    registry = CapabilityRegistry()
    registry.register(manifest)
    registry.activate(manifest.identity.coordinate, manifest.identity.revision)
    provider = StaticProvider(
        (StaticFixture(manifest, {"run": {"ok": True}}),),
        name="static-worker",
    )
    audit = MemoryAuditSink()
    service = CapabilityHubService(
        registry=registry,
        providers=(provider,),
        references=ReferenceSigner(b"process-supervision-test-key"),
        audit=audit,
        provider_supervisor=ProcessProviderSupervisor(),
    )
    context = ServiceContext("tenant", "principal", "session", deadline_ms=5_000)
    budget = BudgetLedger(
        "task",
        {"bytes": 10_000, "executions": 1, "loads": 1, "portable_tokens": 10_000},
    )

    card = service.search("worker", task_id="task", context=context, budget=budget).cards[0]
    loaded = service.load(
        card.capability_ref,
        task_id="task",
        context=context,
        budget=budget,
        operation_names=("run",),
    )
    result = service.execute(
        ExecutionRequest(loaded.execution_ref, "run", {}, "task"),
        context=context,
        budget=budget,
    )

    assert result.output == {"ok": True}
    assert [event.event_type for event in audit.events] == ["search", "load", "execute"]
