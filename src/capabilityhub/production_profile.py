"""Deterministic validation for the conservative production reference profile."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from capabilityhub.models import JsonValue

SCHEMA = "capabilityhub.production-reference.v1"
_MAX_BYTES = 65_536
_PLANES = {"data", "admin"}


def load_production_profile(path: str | Path) -> dict[str, JsonValue]:
    selected = Path(path)
    if selected.stat().st_size > _MAX_BYTES:
        raise ValueError("production profile exceeds the size limit")
    raw = json.loads(selected.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ValueError("production profile must be an object")
    profile = cast(dict[str, JsonValue], raw)
    validate_production_profile(profile)
    return profile


def validate_production_profile(profile: Mapping[str, JsonValue]) -> None:
    if profile.get("schema") != SCHEMA:
        raise ValueError("unsupported production profile schema")
    listeners = _objects(profile, "listeners")
    if {str(item.get("plane")) for item in listeners} != _PLANES:
        raise ValueError("production profile must define separate data and admin planes")
    if any(item.get("tls") != "mutual-required" for item in listeners):
        raise ValueError("every production listener must require mutual TLS")
    if len({str(item.get("bind")) for item in listeners}) != len(listeners):
        raise ValueError("production listeners must have distinct bindings")
    dependencies = _objects(profile, "dependencies")
    if {str(item.get("name")) for item in dependencies} != {
        "registry",
        "index",
        "policy",
        "provider",
    }:
        raise ValueError("all dependency policies must be declared")
    for item in dependencies:
        ttl = item.get("ttl_seconds")
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 300:
            raise ValueError("dependency TTL must be between one and 300 seconds")
        if item.get("on_unknown") != "deny":
            raise ValueError("unknown dependencies must fail closed")
    worker = _object(profile.get("worker"), "worker")
    if worker.get("process_tree_cleanup") != "required":
        raise ValueError("worker process-tree cleanup must be required")
    if worker.get("filesystem_isolation") != "required-or-fail-closed":
        raise ValueError("filesystem isolation must fail closed when unavailable")
    if worker.get("network_isolation") != "required-or-fail-closed":
        raise ValueError("network isolation must fail closed when unavailable")
    supply_chain = _object(profile.get("supply_chain"), "supply_chain")
    required_supply_chain = {
        "bundle": "required",
        "certificate_profile": "code-signing-ed25519",
        "online_checkpoint": "required",
        "checkpoint_observer": "persistent-required",
        "log_growth": "consistency-proof-required",
    }
    if any(supply_chain.get(key) != value for key, value in required_supply_chain.items()):
        raise ValueError("production supply-chain verification must fail closed")
    maximum = supply_chain.get("max_bundle_bytes")
    if isinstance(maximum, bool) or maximum != 131_072:
        raise ValueError("production supply-chain bundle limit is invalid")
    if profile.get("external_credentials") != "not-required-for-validation":
        raise ValueError("reference validation cannot depend on private credentials")


def profile_digest(profile: Mapping[str, JsonValue]) -> str:
    validate_production_profile(profile)
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _objects(profile: Mapping[str, JsonValue], field: str) -> tuple[Mapping[str, Any], ...]:
    value = profile.get(field)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    return tuple(_object(item, field) for item in value)


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} entries must be objects")
    return value
