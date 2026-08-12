"""Process isolation for synchronous capability providers."""

from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import pickle
import platform
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from threading import RLock
from time import monotonic
from typing import Any, Protocol, cast

from capabilityhub.confinement import confinement_status, require_confinement
from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import (
    CapabilityIdentity,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
)
from capabilityhub.providers.base import CapabilityProvider, ProviderContext
from capabilityhub.secret_broker import (
    EnvironmentAliases,
    SecretBrokerError,
    SecretEnvelope,
    seal_worker_secrets,
    secure_worker_transport_available,
    worker_secret_scope,
)

_SPAWN_ENVIRONMENT_LOCK = RLock()


class ProviderSupervisor(Protocol):
    """Run one provider call behind an execution boundary."""

    def execute(
        self,
        provider: CapabilityProvider,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult: ...


@dataclass(frozen=True, slots=True)
class WorkerResourceLimits:
    """Optional hard worker limits; unsupported isolation fails at construction."""

    cpu_seconds: int | None = None
    memory_bytes: int | None = None
    require_filesystem_isolation: bool = False
    require_network_isolation: bool = False
    filesystem_root: str | None = None

    def __post_init__(self) -> None:
        if self.cpu_seconds is not None and self.cpu_seconds < 1:
            raise ValueError("cpu_seconds must be positive")
        if self.memory_bytes is not None and self.memory_bytes < 1_048_576:
            raise ValueError("memory_bytes must be at least 1048576")
        if self.filesystem_root is not None and not os.path.isabs(self.filesystem_root):
            raise ValueError("filesystem_root must be absolute")
        if self.filesystem_root is not None and not self.require_filesystem_isolation:
            raise ValueError("filesystem_root requires filesystem isolation")


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    process_tree: str
    cpu_limit: str
    memory_limit: str
    filesystem_isolation: str | None
    network_isolation: str | None


def sandbox_capabilities() -> SandboxCapabilities:
    """Describe only boundaries the current platform backend can enforce."""

    if os.name == "nt":
        return SandboxCapabilities("windows-job-object", "job-object", "job-object", None, None)
    status = confinement_status()
    return SandboxCapabilities(
        "posix-process-group",
        "setrlimit",
        "setrlimit",
        "landlock" if status.filesystem else None,
        "libseccomp" if status.network else None,
    )


@dataclass(slots=True)
class ProcessProviderSupervisor:
    """Run each call in a spawned process and enforce its wall-clock deadline."""

    start_method: str = "spawn"
    termination_grace_seconds: float = 0.2
    max_message_bytes: int = 4_000_000
    strict_local_providers: bool = False
    resource_limits: WorkerResourceLimits | None = None
    _workers: dict[str, multiprocessing.Process] = field(
        default_factory=dict, init=False, repr=False
    )
    _cancelled: set[str] = field(default_factory=set, init=False, repr=False)
    _jobs: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _receivers: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _worker_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.start_method not in multiprocessing.get_all_start_methods():
            raise ValueError("start_method is not available on this platform")
        if self.termination_grace_seconds < 0:
            raise ValueError("termination_grace_seconds must be non-negative")
        if self.max_message_bytes < 1_024:
            raise ValueError("max_message_bytes must be at least 1024")

    def execute(
        self,
        provider: CapabilityProvider,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        limits = self.resource_limits
        require_confinement(
            filesystem=bool(limits and limits.require_filesystem_isolation),
            network=bool(limits and limits.require_network_isolation),
            filesystem_root=limits.filesystem_root if limits is not None else None,
        )
        if self.strict_local_providers:
            restriction = _local_provider_restriction(provider)
            if restriction is not None:
                raise _error(
                    restriction,
                    ErrorCategory.POLICY,
                    "The provider cannot safely cross the configured process boundary.",
                )
        try:
            aliases = _provider_aliases(provider)
            envelope = _seal_aliases(aliases)
        except SecretBrokerError:
            raise
        try:
            pickle.dumps((provider, identity, request, context, envelope))
        except (pickle.PickleError, TypeError, AttributeError) as error:
            raise _error(
                "provider_worker_not_serializable",
                ErrorCategory.PROVIDER,
                "The provider cannot be transferred to an isolated worker.",
            ) from error

        process_context = multiprocessing.get_context(self.start_method)
        receiver, sender = process_context.Pipe(duplex=False)
        process_factory = cast(Any, process_context).Process
        process = process_factory(
            target=_worker,
            args=(sender, provider, identity, request, context, envelope, self.resource_limits),
            name=f"capabilityhub-{provider.name}",
            daemon=True,
        )
        try:
            with _scrubbed_spawn_environment(aliases):
                process.start()
        except (OSError, RuntimeError, TypeError, AttributeError) as error:
            receiver.close()
            sender.close()
            raise _error(
                "provider_worker_start_failed",
                ErrorCategory.PROVIDER,
                "The isolated provider worker could not be started.",
                retryable=True,
            ) from error
        worker_id = request.execution_ref
        try:
            job = _create_windows_job(process, self.resource_limits)
        except Exception:
            receiver.close()
            sender.close()
            if process.is_alive():
                _stop(process, self.termination_grace_seconds)
            if not process.is_alive():
                process.close()
            raise
        with self._worker_lock:
            if worker_id in self._workers:
                _stop(process, self.termination_grace_seconds)
                receiver.close()
                sender.close()
                raise _error(
                    "provider_worker_conflict",
                    ErrorCategory.CONFLICT,
                    "The isolated provider execution is already active.",
                )
            self._workers[worker_id] = process
            self._receivers[worker_id] = receiver
            if job is not None:
                self._jobs[worker_id] = job
        sender.close()
        timeout_seconds = context.deadline_ms / 1_000
        deadline = monotonic() + timeout_seconds
        try:
            try:
                ready = receiver.poll(timeout_seconds)
            except (OSError, EOFError) as error:
                with self._worker_lock:
                    cancelled = worker_id in self._cancelled
                if cancelled:
                    raise _error(
                        "provider_worker_cancelled",
                        ErrorCategory.CANCELLED,
                        "The isolated provider execution was cancelled.",
                    ) from error
                raise
            if not ready:
                _stop(process, self.termination_grace_seconds)
                raise _error(
                    "provider_worker_timeout",
                    ErrorCategory.TIMEOUT,
                    "The isolated provider worker exceeded its deadline.",
                    retryable=True,
                )
            try:
                raw = receiver.recv_bytes(self.max_message_bytes)
            except EOFError as error:
                process.join(self.termination_grace_seconds)
                with self._worker_lock:
                    cancelled = worker_id in self._cancelled
                code = (
                    "provider_worker_cancelled"
                    if cancelled
                    else "provider_worker_crashed"
                    if process.exitcode not in (None, 0)
                    else "provider_worker_no_result"
                )
                raise _error(
                    code,
                    ErrorCategory.CANCELLED if cancelled else ErrorCategory.PROVIDER,
                    "The isolated provider execution was cancelled."
                    if cancelled
                    else "The isolated provider worker ended without a valid result.",
                    retryable=not cancelled,
                ) from error
            except OSError as error:
                with self._worker_lock:
                    cancelled = worker_id in self._cancelled
                if cancelled:
                    raise _error(
                        "provider_worker_cancelled",
                        ErrorCategory.CANCELLED,
                        "The isolated provider execution was cancelled.",
                    ) from error
                raise _error(
                    "provider_worker_result_too_large",
                    ErrorCategory.BUDGET,
                    "The isolated provider result exceeded the worker message limit.",
                ) from error
            process.join(max(0.0, deadline - monotonic()))
            if process.is_alive():
                _stop(process, self.termination_grace_seconds)
                raise _error(
                    "provider_worker_timeout",
                    ErrorCategory.TIMEOUT,
                    "The isolated provider worker exceeded its deadline.",
                    retryable=True,
                )
        finally:
            receiver.close()
            if process.is_alive():
                _stop(process, self.termination_grace_seconds)
            if not process.is_alive():
                process.close()
            with self._worker_lock:
                self._workers.pop(worker_id, None)
                self._cancelled.discard(worker_id)
                job = self._jobs.pop(worker_id, None)
                self._receivers.pop(worker_id, None)
            _close_windows_handle(job)
        return _decode(raw)

    def cancel(self, execution_ref: str) -> bool:
        """Terminate the registered worker tree for one opaque execution ref."""

        with self._worker_lock:
            process = self._workers.get(execution_ref)
            if process is None or not process.is_alive():
                return False
            self._cancelled.add(execution_ref)
            _close_windows_handle(self._jobs.pop(execution_ref, None))
            _stop(process, self.termination_grace_seconds)
            receiver = self._receivers.get(execution_ref)
            if receiver is not None:
                receiver.close()
            # A true result means the registered cancellation was accepted.  The
            # executing thread owns final join/cleanup, so scheduler lag must not
            # turn an accepted cancellation into a false negative.
            return True

    def active_count(self) -> int:
        """Return an aggregate only; execution references are never exposed."""

        with self._worker_lock:
            return sum(process.is_alive() for process in self._workers.values())


def _worker(
    sender: Connection,
    provider: CapabilityProvider,
    identity: CapabilityIdentity,
    request: ExecutionRequest,
    context: ProviderContext,
    secret_envelope: SecretEnvelope | None,
    resource_limits: WorkerResourceLimits | None,
) -> None:
    try:
        if os.name != "nt" and hasattr(os, "setsid"):
            os.setsid()
        try:
            _apply_resource_limits(resource_limits)
            _apply_worker_confinement(resource_limits)
            with worker_secret_scope(secret_envelope):
                result = provider.execute(identity, request, context)
        except CapabilityHubError as error:
            payload: dict[str, JsonValue] = {
                "category": error.category.value,
                "code": error.code,
                "kind": "error",
                "retryable": error.retryable,
                "safe_message": error.safe_message,
            }
        except BaseException:
            payload = {
                "category": ErrorCategory.PROVIDER.value,
                "code": "provider_worker_failed",
                "kind": "error",
                "retryable": True,
                "safe_message": "The isolated provider failed without a safe structured error.",
            }
        else:
            try:
                payload = {
                    "kind": "result",
                    "result": {
                        "audit_id": result.audit_id,
                        "capability_revision": result.capability_revision,
                        "operation": result.operation,
                        "output": result.output,
                        "portable_tokens": result.portable_tokens,
                        "provider": result.provider,
                    },
                }
            except BaseException:
                payload = {
                    "category": ErrorCategory.PROVIDER.value,
                    "code": "provider_worker_failed",
                    "kind": "error",
                    "retryable": True,
                    "safe_message": "The isolated provider returned an invalid result.",
                }
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            encoded = (
                b'{"category":"provider","code":"provider_worker_failed",'
                b'"kind":"error","retryable":true,"safe_message":'
                b'"The isolated provider returned an invalid result."}'
            )
        sender.send_bytes(encoded)
    finally:
        sender.close()


def _apply_resource_limits(limits: WorkerResourceLimits | None) -> None:
    if limits is None or os.name == "nt":
        return
    resource = cast(Any, importlib.import_module("resource"))
    if limits.cpu_seconds is not None:
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
    if limits.memory_bytes is not None:
        resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))


def _apply_worker_confinement(limits: WorkerResourceLimits | None) -> None:
    if limits is None or not (
        limits.require_filesystem_isolation or limits.require_network_isolation
    ):
        return
    if os.name != "posix" or platform.system() != "Linux":
        raise _isolation_error("provider_os_confinement_unavailable")
    from capabilityhub.linux_sandbox import LinuxSandboxApplyError, apply_linux_sandbox

    try:
        apply_linux_sandbox(
            filesystem_root=limits.filesystem_root
            if limits.require_filesystem_isolation
            else None,
            deny_network=limits.require_network_isolation,
        )
    except LinuxSandboxApplyError as error:
        raise _isolation_error(f"provider_{error.stage}") from error
    except (OSError, RuntimeError, ValueError) as error:
        raise _isolation_error("provider_os_confinement_apply_failed") from error


def _create_windows_job(
    process: multiprocessing.Process, limits: WorkerResourceLimits | None
) -> int | None:
    if os.name != "nt":
        return None
    ctypes = cast(Any, importlib.import_module("ctypes"))
    wintypes = cast(Any, importlib.import_module("ctypes.wintypes"))

    io_counters = type(
        "_IoCounters",
        (ctypes.Structure,),
        {"_fields_": tuple((name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        ))},
    )
    basic_limit = type(
        "_BasicLimit",
        (ctypes.Structure,),
        {"_fields_": (
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )},
    )
    extended_limit = type(
        "_ExtendedLimit",
        (ctypes.Structure,),
        {"_fields_": (
            ("BasicLimitInformation", basic_limit),
            ("IoInfo", io_counters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )},
    )

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise _isolation_error("provider_worker_job_create_failed")
    information = extended_limit()
    information.BasicLimitInformation.LimitFlags = 0x2000
    if limits is not None and limits.cpu_seconds is not None:
        information.BasicLimitInformation.LimitFlags |= 0x2
        information.BasicLimitInformation.PerProcessUserTimeLimit = limits.cpu_seconds * 10_000_000
    if limits is not None and limits.memory_bytes is not None:
        information.BasicLimitInformation.LimitFlags |= 0x100
        information.ProcessMemoryLimit = limits.memory_bytes
    configured = kernel32.SetInformationJobObject(
        handle, 9, ctypes.byref(information), ctypes.sizeof(information)
    )
    process_handle = cast(Any, process)._popen._handle
    assigned = configured and kernel32.AssignProcessToJobObject(handle, process_handle)
    if not assigned:
        kernel32.CloseHandle(handle)
        _stop(process, 0.2)
        raise _isolation_error("provider_worker_job_assignment_failed")
    return cast(int, handle)


def _close_windows_handle(handle: int | None) -> None:
    if handle is None or os.name != "nt":
        return
    ctypes = cast(Any, importlib.import_module("ctypes"))
    wintypes = cast(Any, importlib.import_module("ctypes.wintypes"))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle(handle)


def _isolation_error(code: str) -> CapabilityHubError:
    return _error(
        code,
        ErrorCategory.POLICY,
        "The required worker isolation boundary could not be enforced.",
    )


def _decode(raw: bytes) -> ExecutionResult:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _error(
            "provider_worker_protocol_error",
            ErrorCategory.PROVIDER,
            "The isolated provider worker returned an invalid envelope.",
        ) from error
    if not isinstance(payload, dict):
        raise _protocol_error()
    if payload.get("kind") == "error":
        try:
            category = ErrorCategory(payload["category"])
        except (KeyError, ValueError, TypeError) as error:
            raise _protocol_error() from error
        code = payload.get("code")
        safe_message = payload.get("safe_message")
        retryable = payload.get("retryable", False)
        if (
            not isinstance(code, str)
            or not isinstance(safe_message, str)
            or not isinstance(retryable, bool)
        ):
            raise _protocol_error()
        raise CapabilityHubError(
            code=code[:128],
            category=category,
            safe_message=safe_message[:1_000],
            retryable=retryable,
        )
    result = payload.get("result")
    if payload.get("kind") != "result" or not isinstance(result, Mapping):
        raise _protocol_error()
    try:
        capability_revision = result["capability_revision"]
        operation = result["operation"]
        output = cast(JsonValue, result["output"])
        provider = result["provider"]
        portable_tokens = result["portable_tokens"]
        audit_id = result["audit_id"]
    except KeyError as error:
        raise _protocol_error() from error
    if (
        not isinstance(capability_revision, str)
        or not isinstance(operation, str)
        or not isinstance(provider, str)
        or not isinstance(portable_tokens, int)
        or isinstance(portable_tokens, bool)
        or not isinstance(audit_id, str)
    ):
        raise _protocol_error()
    return ExecutionResult(
        capability_revision=capability_revision,
        operation=operation,
        output=output,
        provider=provider,
        portable_tokens=portable_tokens,
        audit_id=audit_id,
    )


def _stop(process: multiprocessing.Process, grace_seconds: float) -> None:
    if process.is_alive() and os.name == "nt" and process.pid is not None:
        try:
            subprocess.run(
                ("taskkill.exe", "/PID", str(process.pid), "/T", "/F"),
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(grace_seconds, 1.0),
            )
        except (OSError, subprocess.SubprocessError):
            process.terminate()
        process.join(grace_seconds)
    elif process.is_alive() and process.pid is not None:
        try:
            posix_os = cast(Any, os)
            if posix_os.getpgid(process.pid) == process.pid:
                posix_os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            process.terminate()
        process.join(grace_seconds)
    if process.is_alive() and hasattr(process, "kill"):
        if os.name != "nt" and process.pid is not None:
            try:
                posix_os = cast(Any, os)
                posix_signal = cast(Any, signal)
                if posix_os.getpgid(process.pid) == process.pid:
                    posix_os.killpg(process.pid, posix_signal.SIGKILL)
                else:
                    process.kill()
            except (OSError, ProcessLookupError):
                process.kill()
        else:
            process.kill()
        process.join(grace_seconds)


def _local_provider_restriction(provider: CapabilityProvider) -> str | None:
    """Allow known local adapters only when their serialized state has no plaintext secret."""

    from capabilityhub.providers.cli import CliProcessProvider
    from capabilityhub.providers.http import EnvironmentHeaders, HttpApiProvider
    from capabilityhub.providers.mcp import McpStdioProvider
    from capabilityhub.providers.rag import LocalRagProvider
    from capabilityhub.providers.static import StaticProvider

    if isinstance(provider, HttpApiProvider):
        for http_fixture in provider._fixtures:
            headers = http_fixture.headers
            if headers is None:
                continue
            if isinstance(headers, EnvironmentHeaders):
                if headers.broker_factory is not None:
                    return "provider_worker_header_supplier_unsupported"
                if not secure_worker_transport_available():
                    return "provider_worker_secret_boundary_unsupported"
                continue
            return "provider_worker_header_supplier_unsupported"
        return None
    if isinstance(provider, CliProcessProvider):
        for cli_fixture in provider._fixtures:
            environment = cli_fixture.environment
            if environment and not isinstance(environment, EnvironmentAliases):
                return "provider_worker_plaintext_environment_denied"
            if environment and not secure_worker_transport_available():
                return "provider_worker_secret_boundary_unsupported"
        return None
    if isinstance(provider, McpStdioProvider):
        for mcp_fixture in provider._fixtures:
            environment = mcp_fixture.environment
            if environment and not isinstance(environment, EnvironmentAliases):
                return "provider_worker_plaintext_environment_denied"
            if environment and not secure_worker_transport_available():
                return "provider_worker_secret_boundary_unsupported"
        return None
    if isinstance(provider, (LocalRagProvider, StaticProvider)):
        return None
    return "provider_worker_type_unsupported"


def _provider_aliases(provider: CapabilityProvider) -> tuple[str, ...]:
    from capabilityhub.providers.cli import CliProcessProvider
    from capabilityhub.providers.http import EnvironmentHeaders, HttpApiProvider
    from capabilityhub.providers.mcp import McpStdioProvider

    aliases: list[str] = []
    if isinstance(provider, HttpApiProvider):
        for http_fixture in provider._fixtures:
            if isinstance(http_fixture.headers, EnvironmentHeaders):
                aliases.extend(alias for _header, alias in http_fixture.headers.sources)
    elif isinstance(provider, CliProcessProvider):
        for cli_fixture in provider._fixtures:
            if isinstance(cli_fixture.environment, EnvironmentAliases):
                aliases.extend(cli_fixture.environment.aliases)
    elif isinstance(provider, McpStdioProvider):
        for mcp_fixture in provider._fixtures:
            if isinstance(mcp_fixture.environment, EnvironmentAliases):
                aliases.extend(mcp_fixture.environment.aliases)
    return tuple(dict.fromkeys(aliases))


def _seal_aliases(aliases: tuple[str, ...]) -> SecretEnvelope | None:
    if not aliases:
        return None
    values: dict[str, str] = {}
    for alias in aliases:
        value = os.environ.get(alias)
        if not value:
            raise SecretBrokerError("secret_alias_unavailable")
        values[alias] = value
    try:
        return seal_worker_secrets(values)
    finally:
        values.clear()


class _scrubbed_spawn_environment:
    def __init__(self, aliases: tuple[str, ...]) -> None:
        self._aliases = aliases
        self._saved: dict[str, str] = {}

    def __enter__(self) -> None:
        _SPAWN_ENVIRONMENT_LOCK.acquire()
        for alias in self._aliases:
            value = os.environ.pop(alias, None)
            if value is not None:
                self._saved[alias] = value

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        os.environ.update(self._saved)
        self._saved.clear()
        _SPAWN_ENVIRONMENT_LOCK.release()


def _protocol_error() -> CapabilityHubError:
    return _error(
        "provider_worker_protocol_error",
        ErrorCategory.PROVIDER,
        "The isolated provider worker returned an invalid envelope.",
    )


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool = False,
) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=category,
        safe_message=message,
        retryable=retryable,
    )
