"""Process isolation for synchronous capability providers."""

from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import pickle
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from threading import RLock
from time import monotonic
from typing import Any, Protocol, cast

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import (
    CapabilityIdentity,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
)
from capabilityhub.providers.base import CapabilityProvider, ProviderContext
from capabilityhub.secret_broker import (
    DpapiSecretEnvelope,
    EnvironmentAliases,
    SecretBrokerError,
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

    def __post_init__(self) -> None:
        if self.cpu_seconds is not None and self.cpu_seconds < 1:
            raise ValueError("cpu_seconds must be positive")
        if self.memory_bytes is not None and self.memory_bytes < 1_048_576:
            raise ValueError("memory_bytes must be at least 1048576")
        if self.require_filesystem_isolation or self.require_network_isolation:
            raise ValueError("filesystem and network isolation are not supported")
        if os.name == "nt" and (self.cpu_seconds is not None or self.memory_bytes is not None):
            raise ValueError("CPU and memory worker limits are not supported on Windows")


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
        sender.close()
        timeout_seconds = context.deadline_ms / 1_000
        deadline = monotonic() + timeout_seconds
        try:
            if not receiver.poll(timeout_seconds):
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
                    ErrorCategory.TIMEOUT if cancelled else ErrorCategory.PROVIDER,
                    "The isolated provider execution was cancelled."
                    if cancelled
                    else "The isolated provider worker ended without a valid result.",
                    retryable=not cancelled,
                ) from error
            except OSError as error:
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
        return _decode(raw)

    def cancel(self, execution_ref: str) -> bool:
        """Terminate the registered worker tree for one opaque execution ref."""

        with self._worker_lock:
            process = self._workers.get(execution_ref)
            if process is None or not process.is_alive():
                return False
            self._cancelled.add(execution_ref)
            _stop(process, self.termination_grace_seconds)
            return not process.is_alive()

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
    secret_envelope: DpapiSecretEnvelope | None,
    resource_limits: WorkerResourceLimits | None,
) -> None:
    try:
        if os.name != "nt" and hasattr(os, "setsid"):
            os.setsid()
        _apply_resource_limits(resource_limits)
        try:
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
                if not DpapiSecretEnvelope.available():
                    return "provider_worker_secret_boundary_unsupported"
                continue
            return "provider_worker_header_supplier_unsupported"
        return None
    if isinstance(provider, CliProcessProvider):
        for cli_fixture in provider._fixtures:
            environment = cli_fixture.environment
            if environment and not isinstance(environment, EnvironmentAliases):
                return "provider_worker_plaintext_environment_denied"
            if environment and not DpapiSecretEnvelope.available():
                return "provider_worker_secret_boundary_unsupported"
        return None
    if isinstance(provider, McpStdioProvider):
        for mcp_fixture in provider._fixtures:
            environment = mcp_fixture.environment
            if environment and not isinstance(environment, EnvironmentAliases):
                return "provider_worker_plaintext_environment_denied"
            if environment and not DpapiSecretEnvelope.available():
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


def _seal_aliases(aliases: tuple[str, ...]) -> DpapiSecretEnvelope | None:
    if not aliases:
        return None
    values: dict[str, str] = {}
    for alias in aliases:
        value = os.environ.get(alias)
        if not value:
            raise SecretBrokerError("secret_alias_unavailable")
        values[alias] = value
    try:
        return DpapiSecretEnvelope.seal(values)
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
