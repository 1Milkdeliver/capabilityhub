"""Deny-by-default parameter authorization shared by search and execution."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from capabilityhub.models import CapabilityManifest, JsonValue

_FILESYSTEM_PERMISSIONS = frozenset({"filesystem", "filesystem.read", "filesystem.write"})
_NETWORK_PERMISSIONS = frozenset({"network", "network.http"})
_PROCESS_PERMISSIONS = frozenset({"process", "process.execute"})
_SECRET_PERMISSIONS = frozenset({"secret", "secret.use"})
_UNCONSTRAINED_PERMISSIONS = frozenset({"content.sensitive"})
_SUPPORTED_PERMISSIONS = frozenset().union(
    _FILESYSTEM_PERMISSIONS,
    _NETWORK_PERMISSIONS,
    _PROCESS_PERMISSIONS,
    _SECRET_PERMISSIONS,
    _UNCONSTRAINED_PERMISSIONS,
)
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}$")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_RAW_SECRET_FIELDS = frozenset(
    {"api_key", "authorization", "credential", "credentials", "secret", "secret_value", "token"}
)
_REASON_ORDER = {
    "unknown_permission": 0,
    "permission_not_granted": 1,
    "permission_constraint_missing": 2,
    "arguments_invalid": 3,
    "privilege_not_declared": 4,
    "path_outside_allowed_roots": 5,
    "host_not_allowed": 6,
    "http_method_not_allowed": 7,
    "command_not_allowed": 8,
    "profile_not_allowed": 9,
    "secret_value_not_allowed": 10,
    "secret_alias_not_allowed": 11,
}


@dataclass(frozen=True, slots=True)
class PermissionConstraint:
    path_roots: tuple[str | Path, ...] = ()
    hosts: frozenset[str] = field(default_factory=frozenset)
    http_methods: frozenset[str] = field(default_factory=frozenset)
    commands: frozenset[str] = field(default_factory=frozenset)
    profiles: frozenset[str] = field(default_factory=frozenset)
    secret_aliases: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason_codes: tuple[str, ...]
    required_permissions: tuple[str, ...]
    effective_permissions: tuple[str, ...]


class ParameterAuthorizer:
    """Intersect declared requirements with constrained caller grants."""

    def __init__(self, grants: Mapping[str, PermissionConstraint]) -> None:
        normalized: dict[str, PermissionConstraint] = {}
        for permission, constraint in grants.items():
            if permission not in _SUPPORTED_PERMISSIONS:
                raise ValueError("grant contains an unsupported permission")
            normalized[permission] = _normalize_constraint(permission, constraint)
        self._grants = normalized

    @property
    def granted_permissions(self) -> frozenset[str]:
        """Return permission names only; constraints and sensitive values stay private."""

        return frozenset(self._grants)

    def eligible(
        self,
        manifest: CapabilityManifest,
        dependencies: Iterable[CapabilityManifest] = (),
    ) -> AuthorizationDecision:
        return self._decide(manifest, dependencies, None)

    def authorize(
        self,
        manifest: CapabilityManifest,
        *,
        dependencies: Iterable[CapabilityManifest] = (),
        normalized_arguments: Mapping[str, JsonValue],
    ) -> AuthorizationDecision:
        return self._decide(manifest, dependencies, normalized_arguments)

    def _decide(
        self,
        manifest: CapabilityManifest,
        dependencies: Iterable[CapabilityManifest],
        arguments: Mapping[str, JsonValue] | None,
    ) -> AuthorizationDecision:
        required = set(manifest.permissions)
        for dependency in dependencies:
            required.update(dependency.permissions)
        effective = required.intersection(self._grants)
        reasons: list[str] = []
        if required.difference(_SUPPORTED_PERMISSIONS):
            reasons.append("unknown_permission")
        if required.difference(self._grants):
            reasons.append("permission_not_granted")
        if arguments is not None and not reasons:
            reasons.extend(self._argument_reasons(required, arguments))
        ordered = _ordered_reasons(reasons)
        return AuthorizationDecision(
            allowed=not ordered,
            reason_codes=ordered or ("authorization_allow",),
            required_permissions=tuple(sorted(required.intersection(_SUPPORTED_PERMISSIONS))),
            effective_permissions=tuple(sorted(effective)),
        )

    def _argument_reasons(
        self, required: set[str], arguments: Mapping[str, JsonValue]
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        filesystem = tuple(sorted(required.intersection(_FILESYSTEM_PERMISSIONS)))
        network = tuple(sorted(required.intersection(_NETWORK_PERMISSIONS)))
        process = tuple(sorted(required.intersection(_PROCESS_PERMISSIONS)))
        secrets = tuple(sorted(required.intersection(_SECRET_PERMISSIONS)))
        if "path" in arguments and not filesystem:
            reasons.append("privilege_not_declared")
        if ({"host", "http_method"} & arguments.keys()) and not network:
            reasons.append("privilege_not_declared")
        if ({"command", "profile"} & arguments.keys()) and not process:
            reasons.append("privilege_not_declared")
        if "secret_alias" in arguments and not secrets:
            reasons.append("privilege_not_declared")
        if any(str(name).casefold() in _RAW_SECRET_FIELDS for name in arguments):
            reasons.append("secret_value_not_allowed")
        if filesystem:
            reasons.extend(self._path_reasons(filesystem, arguments.get("path")))
        if network:
            reasons.extend(
                self._network_reasons(
                    network,
                    arguments.get("host"),
                    arguments.get("http_method"),
                )
            )
        if process:
            reasons.extend(
                self._process_reasons(
                    process,
                    arguments.get("command"),
                    arguments.get("profile"),
                )
            )
        if secrets:
            reasons.extend(self._secret_reasons(secrets, arguments.get("secret_alias")))
        return _ordered_reasons(reasons)

    def _path_reasons(self, permissions: tuple[str, ...], raw_path: JsonValue) -> tuple[str, ...]:
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            return ("arguments_invalid",)
        try:
            selected = Path(raw_path).resolve()
        except OSError:
            return ("path_outside_allowed_roots",)
        for permission in permissions:
            roots = self._grants[permission].path_roots
            if not any(selected.is_relative_to(Path(root)) for root in roots):
                return ("path_outside_allowed_roots",)
        return ()

    def _network_reasons(
        self,
        permissions: tuple[str, ...],
        raw_host: JsonValue,
        raw_method: JsonValue,
    ) -> tuple[str, ...]:
        if not isinstance(raw_host, str) or not isinstance(raw_method, str):
            return ("arguments_invalid",)
        try:
            host = _host(raw_host)
        except ValueError:
            return ("host_not_allowed",)
        method = raw_method.upper()
        reasons: list[str] = []
        if any(host not in self._grants[name].hosts for name in permissions):
            reasons.append("host_not_allowed")
        if any(method not in self._grants[name].http_methods for name in permissions):
            reasons.append("http_method_not_allowed")
        return _ordered_reasons(reasons)

    def _process_reasons(
        self,
        permissions: tuple[str, ...],
        command: JsonValue,
        profile: JsonValue,
    ) -> tuple[str, ...]:
        if not isinstance(command, str) or not isinstance(profile, str):
            return ("arguments_invalid",)
        reasons: list[str] = []
        if any(command not in self._grants[name].commands for name in permissions):
            reasons.append("command_not_allowed")
        if any(profile not in self._grants[name].profiles for name in permissions):
            reasons.append("profile_not_allowed")
        return _ordered_reasons(reasons)

    def _secret_reasons(self, permissions: tuple[str, ...], alias: JsonValue) -> tuple[str, ...]:
        if not isinstance(alias, str):
            return ("arguments_invalid",)
        if any(alias not in self._grants[name].secret_aliases for name in permissions):
            return ("secret_alias_not_allowed",)
        return ()


def _normalize_constraint(
    permission: str, constraint: PermissionConstraint
) -> PermissionConstraint:
    roots = tuple(_root(value) for value in constraint.path_roots)
    hosts = frozenset(_host(value) for value in constraint.hosts)
    methods = frozenset(value.upper() for value in constraint.http_methods)
    commands = frozenset(_name(value) for value in constraint.commands)
    profiles = frozenset(_name(value) for value in constraint.profiles)
    aliases = frozenset(_name(value) for value in constraint.secret_aliases)
    if not methods <= _HTTP_METHODS:
        raise ValueError("grant contains an unsupported HTTP method")
    populated = {
        "filesystem": bool(roots),
        "network": bool(hosts and methods),
        "process": bool(commands and profiles),
        "secret": bool(aliases),
        "unconstrained": not any((roots, hosts, methods, commands, profiles, aliases)),
    }
    category = _permission_category(permission)
    if not populated[category]:
        raise ValueError("grant is missing its required constraint")
    allowed_fields = {
        "filesystem": (roots,),
        "network": (hosts, methods),
        "process": (commands, profiles),
        "secret": (aliases,),
        "unconstrained": (),
    }[category]
    populated_values = tuple(
        value for value in (roots, hosts, methods, commands, profiles, aliases) if value
    )
    if populated_values != allowed_fields:
        raise ValueError("grant contains constraints for another permission category")
    return PermissionConstraint(roots, hosts, methods, commands, profiles, aliases)


def _permission_category(permission: str) -> str:
    if permission in _FILESYSTEM_PERMISSIONS:
        return "filesystem"
    if permission in _NETWORK_PERMISSIONS:
        return "network"
    if permission in _PROCESS_PERMISSIONS:
        return "process"
    if permission in _SECRET_PERMISSIONS:
        return "secret"
    return "unconstrained"


def _root(value: str | Path) -> Path:
    selected = Path(value)
    if not selected.is_absolute():
        raise ValueError("path roots must be absolute")
    return selected.resolve()


def _host(value: str) -> str:
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ValueError("hosts must be non-empty names without whitespace")
    selected = value.rstrip(".").casefold()
    if "://" in selected or "/" in selected or "@" in selected:
        raise ValueError("hosts must not contain URL components")
    try:
        return str(ipaddress.ip_address(selected))
    except ValueError:
        try:
            encoded = selected.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise ValueError("host is invalid") from error
        if len(encoded) > 253 or any(
            _HOST_LABEL.fullmatch(label) is None for label in encoded.split(".")
        ):
            raise ValueError("host is invalid") from None
        return encoded


def _name(value: str) -> str:
    if not isinstance(value, str) or _SAFE_NAME.fullmatch(value) is None:
        raise ValueError("constraint names must use the safe identifier form")
    return value


def _ordered_reasons(reasons: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(reasons), key=lambda value: (_REASON_ORDER.get(value, 999), value)))
