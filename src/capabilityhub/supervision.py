"""Process isolation for synchronous capability providers."""

from __future__ import annotations

import json
import multiprocessing
import pickle
from collections.abc import Mapping
from dataclasses import dataclass
from multiprocessing.connection import Connection
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
class ProcessProviderSupervisor:
    """Run each call in a spawned process and enforce its wall-clock deadline."""

    start_method: str = "spawn"
    termination_grace_seconds: float = 0.2
    max_message_bytes: int = 4_000_000
    strict_local_providers: bool = False

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
            pickle.dumps((provider, identity, request, context))
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
            args=(sender, provider, identity, request, context),
            name=f"capabilityhub-{provider.name}",
            daemon=True,
        )
        try:
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
                code = (
                    "provider_worker_crashed"
                    if process.exitcode not in (None, 0)
                    else "provider_worker_no_result"
                )
                raise _error(
                    code,
                    ErrorCategory.PROVIDER,
                    "The isolated provider worker ended without a valid result.",
                    retryable=True,
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
        return _decode(raw)


def _worker(
    sender: Connection,
    provider: CapabilityProvider,
    identity: CapabilityIdentity,
    request: ExecutionRequest,
    context: ProviderContext,
) -> None:
    try:
        try:
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
    if process.is_alive():
        process.terminate()
        process.join(grace_seconds)
    if process.is_alive() and hasattr(process, "kill"):
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
        for fixture in provider._fixtures:
            headers = fixture.headers
            if headers is None:
                continue
            if isinstance(headers, EnvironmentHeaders):
                return "provider_worker_secret_boundary_unsupported"
            return "provider_worker_header_supplier_unsupported"
        return None
    if isinstance(provider, (CliProcessProvider, McpStdioProvider)):
        if any(
            fixture.environment
            for fixture in provider._fixtures
        ):
            return "provider_worker_plaintext_environment_denied"
        return None
    if isinstance(provider, (LocalRagProvider, StaticProvider)):
        return None
    return "provider_worker_type_unsupported"


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
