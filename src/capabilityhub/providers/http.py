"""Fixed-origin, bounded HTTP JSON API provider."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.metering import canonical_json, measure_text
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    JsonValue,
)
from capabilityhub.providers.base import ProviderContext
from capabilityhub.secret_broker import (
    ScopedSecretBroker,
    SecretConsumer,
    SecretConsumerContext,
    SecretScope,
    resolve_worker_alias,
)

_PATH_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_METHODS = frozenset(("GET", "POST", "PUT", "PATCH", "DELETE"))
BrokerFactory = Callable[[Mapping[str, SecretConsumer]], ScopedSecretBroker]


@dataclass(frozen=True, slots=True)
class HttpInvocation:
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    body: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.method not in _METHODS:
            raise ValueError("HTTP method is not allowlisted")
        if not self.path.startswith("/") or "://" in self.path or "?" in self.path:
            raise ValueError("HTTP path must be an absolute path without a host or query")
        if self.method in {"GET", "DELETE"} and self.body:
            raise ValueError("GET and DELETE invocations do not accept a JSON body")


@dataclass(frozen=True, slots=True)
class HttpApiFixture:
    manifest: CapabilityManifest
    base_url: str
    operations: Mapping[str, HttpInvocation]
    headers: Callable[[], Mapping[str, str]] | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("HTTP base URL must use http or https and include a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("HTTP base URL must not contain credentials, query, or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Cleartext HTTP is allowed only for loopback fixtures")
        declared = {operation.name for operation in self.manifest.operations}
        if not self.operations or not set(self.operations).issubset(declared):
            raise ValueError("HTTP operations must be non-empty and declared by the manifest")


@dataclass(frozen=True, slots=True)
class EnvironmentHeaders:
    """Resolve header aliases through a one-use scoped broker at invocation time."""

    sources: tuple[tuple[str, str], ...]
    broker_factory: BrokerFactory | None = field(default=None, repr=False, compare=False)

    def __call__(self) -> Mapping[str, str]:
        """Legacy direct resolution for callers outside provider execution."""

        headers: dict[str, str] = {}
        for header, environment_name in self.sources:
            value = resolve_worker_alias(environment_name)
            if value is None:
                value = os.environ.get(environment_name)
            if value is None:
                raise _error(
                    "http_header_environment_missing",
                    ErrorCategory.INPUT,
                    "A required HTTP header environment variable is unavailable.",
                )
            headers[header] = value
        return headers

    def resolve(
        self,
        *,
        provider: str,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> Mapping[str, str]:
        """Keep plaintext inside a request-local trusted consumer only."""

        headers: dict[str, str] = {}
        for header, alias in self.sources:
            def consume(
                value: str,
                _secret_context: SecretConsumerContext,
                *,
                target: str = header,
            ) -> None:
                headers[target] = value

            consumers = {provider: consume}
            broker = (
                self.broker_factory(consumers)
                if self.broker_factory is not None
                else ScopedSecretBroker(
                    consumers,
                    environment=lambda selected: resolve_worker_alias(selected)
                    or os.environ.get(selected),
                )
            )
            scope = SecretScope(
                tenant=context.tenant_id,
                principal=context.principal_id,
                session=context.session_id,
                task=request.task_id,
                provider=provider,
                operation=request.operation,
                policy_revision="local-header-alias-v1",
            )
            handle = broker.issue(alias, scope=scope, ttl_seconds=5, max_uses=1)
            broker.consume(handle, scope=scope)
        return headers


class HttpApiProvider:
    """Invoke configured JSON endpoints without exposing a generic URL fetch primitive."""

    def __init__(
        self,
        fixtures: tuple[HttpApiFixture, ...] | list[HttpApiFixture],
        *,
        name: str = "http-api",
    ) -> None:
        if not name:
            raise ValueError("HTTP provider name must not be empty")
        self._name = name
        values = tuple(fixtures)
        revisions = [fixture.manifest.identity.revision for fixture in values]
        if len(revisions) != len(set(revisions)):
            raise ValueError("HTTP fixture revisions must be unique")
        if any(fixture.manifest.provider != name for fixture in values):
            raise ValueError("HTTP manifest provider must match the configured provider name")
        self._fixtures = values
        self._by_revision = {fixture.manifest.identity.revision: fixture for fixture in values}
        self._opener = build_opener(_RejectRedirects())

    def __getstate__(self) -> dict[str, object]:
        """Exclude urllib runtime state when transferring to a spawned worker."""

        return {"name": self._name, "fixtures": self._fixtures}

    def __setstate__(self, state: Mapping[str, object]) -> None:
        self._name = cast(str, state["name"])
        self._fixtures = cast(tuple[HttpApiFixture, ...], state["fixtures"])
        self._by_revision = {
            fixture.manifest.identity.revision: fixture for fixture in self._fixtures
        }
        self._opener = build_opener(_RejectRedirects())

    @property
    def name(self) -> str:
        return self._name

    def discover(self) -> tuple[CapabilityManifest, ...]:
        return tuple(fixture.manifest for fixture in self._fixtures)

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        context: ProviderContext,
    ) -> ExecutionResult:
        fixture = self._by_revision.get(identity.revision)
        if fixture is None:
            raise _error(
                "http_capability_not_found",
                ErrorCategory.REFERENCE,
                "The requested HTTP capability is not configured.",
            )
        invocation = fixture.operations.get(request.operation)
        if invocation is None:
            raise _error(
                "http_operation_not_found",
                ErrorCategory.REFERENCE,
                "The requested HTTP operation is not allowlisted.",
            )
        url = _url(fixture.base_url, invocation, request.arguments)
        headers = {"Accept": "application/json", "User-Agent": "CapabilityHub/0.1"}
        secret_canaries: tuple[str, ...] = ()
        if fixture.headers is not None:
            supplied = (
                fixture.headers.resolve(
                    provider=self.name,
                    request=request,
                    context=context,
                )
                if isinstance(fixture.headers, EnvironmentHeaders)
                else fixture.headers()
            )
            if isinstance(fixture.headers, EnvironmentHeaders):
                secret_canaries = tuple(supplied.values())
            headers.update(_safe_headers(supplied))
        data: bytes | None = None
        if invocation.body:
            data = canonical_json(
                {key: _argument(request.arguments, key) for key in invocation.body}
            ).encode()
            headers["Content-Type"] = "application/json"
        http_request = Request(url, data=data, headers=headers, method=invocation.method)
        max_bytes = context.max_output_tokens * 4
        try:
            with self._opener.open(
                http_request,
                timeout=context.deadline_ms / 1000,
            ) as response:
                raw = response.read(max_bytes + 1)
                status = response.status
        except HTTPError as error:
            raise _error(
                "http_response_error",
                ErrorCategory.PROVIDER,
                "The HTTP API returned an error response.",
                details={"status": error.code},
            ) from error
        except (TimeoutError, URLError) as error:
            reason = getattr(error, "reason", None)
            timeout = isinstance(error, TimeoutError) or isinstance(reason, TimeoutError)
            raise _error(
                "http_deadline_exceeded" if timeout else "http_connection_failed",
                ErrorCategory.TIMEOUT if timeout else ErrorCategory.PROVIDER,
                "The HTTP API request timed out."
                if timeout
                else "The HTTP API could not be reached.",
                retryable=True,
            ) from error
        if len(raw) > max_bytes:
            raise _error(
                "http_output_budget_exceeded",
                ErrorCategory.BUDGET,
                "The HTTP response exceeded the hard output budget.",
            )
        if any(value and value.encode() in raw for value in secret_canaries):
            raise _error(
                "http_secret_canary_detected",
                ErrorCategory.POLICY,
                "The HTTP response contained protected credential material.",
            )
        try:
            output = cast(JsonValue, json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _error(
                "http_invalid_json",
                ErrorCategory.PROVIDER,
                "The HTTP API returned invalid JSON.",
            ) from error
        serialized = canonical_json(output)
        audit_material = canonical_json(
            {
                "operation": request.operation,
                "revision": identity.revision,
                "status": status,
                "task_id": request.task_id,
            }
        ).encode()
        return ExecutionResult(
            capability_revision=identity.revision,
            operation=request.operation,
            output=output,
            provider=self.name,
            portable_tokens=measure_text(serialized).portable_tokens,
            audit_id=f"http-{hashlib.sha256(audit_material).hexdigest()[:16]}",
        )


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _url(base_url: str, invocation: HttpInvocation, arguments: Mapping[str, JsonValue]) -> str:
    def replace(match: re.Match[str]) -> str:
        return quote(str(_scalar_argument(arguments, match.group(1))), safe="")

    path = _PATH_PARAMETER.sub(replace, invocation.path)
    query = urlencode(
        {
            target: str(_scalar_argument(arguments, source))
            for source, target in invocation.query.items()
        }
    )
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    return f"{url}?{query}" if query else url


def _argument(arguments: Mapping[str, JsonValue], key: str) -> JsonValue:
    if key not in arguments:
        raise _error(
            "http_argument_missing",
            ErrorCategory.INPUT,
            "A required HTTP argument is missing.",
            details={"argument": key},
        )
    return arguments[key]


def _scalar_argument(arguments: Mapping[str, JsonValue], key: str) -> str | int | float | bool:
    value = _argument(arguments, key)
    if not isinstance(value, (str, int, float, bool)):
        raise _error(
            "http_argument_not_scalar",
            ErrorCategory.INPUT,
            "A path or query HTTP argument must be scalar.",
            details={"argument": key},
        )
    return value


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str) or "\n" in name + value:
            raise _error(
                "http_header_invalid",
                ErrorCategory.INPUT,
                "An out-of-band HTTP header is invalid.",
            )
        safe[name] = value
    return safe


def _error(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, object] | None = None,
) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=category,
        safe_message=message,
        retryable=retryable,
        details=details or {},
    )
