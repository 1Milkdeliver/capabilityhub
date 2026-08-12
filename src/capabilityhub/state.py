"""Small, atomic local preference and lifecycle store."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, cast

from capabilityhub.errors import CapabilityHubError, ErrorCategory

Locale = Literal["auto", "en", "zh-CN"]
LifecycleState = Literal["enabled", "disabled", "quarantined"]
PreferenceScope = Literal["project", "global"]

_LOCALES = frozenset(("auto", "en", "zh-CN"))
_STATES = frozenset(("enabled", "disabled", "quarantined"))


def global_config_path(home: Path | None = None) -> Path:
    """Return the platform-local global config path without creating it."""

    home_dir = (home or Path.home()).resolve()
    if os.name == "nt":
        base = (
            home_dir / "AppData" / "Roaming"
            if home is not None
            else Path(os.environ.get("APPDATA", home_dir / "AppData" / "Roaming"))
        )
    else:
        base = (
            home_dir / ".config"
            if home is not None
            else Path(os.environ.get("XDG_CONFIG_HOME", home_dir / ".config"))
        )
    return base / "capabilityhub" / "config.json"


def project_config_path(project: Path) -> Path:
    return project.resolve() / ".capabilityhub" / "config.json"


def resolved_preferences(
    *, home: Path | None = None, project: Path | None = None
) -> dict[str, object]:
    """Resolve project-over-global preferences while preserving a compact shape."""

    home_dir = (home or Path.home()).resolve()
    project_dir = (project or Path.cwd()).resolve()
    global_payload = _read(global_config_path(home_dir))
    project_payload = _read(project_config_path(project_dir))
    global_states = _states(global_payload)
    project_states = _states(project_payload)
    states = {**global_states, **project_states}
    locale = _locale(project_payload) or _locale(global_payload) or "auto"
    return {
        "capabilities": states,
        "locale": locale,
        "paths": {
            "global": str(global_config_path(home_dir)),
            "project": str(project_config_path(project_dir)),
        },
    }


def set_locale(
    locale: str,
    *,
    scope: PreferenceScope,
    home: Path | None = None,
    project: Path | None = None,
) -> Path:
    if locale not in _LOCALES:
        raise _input("invalid_locale", "Locale must be auto, en, or zh-CN.")
    path = _scope_path(scope, home=home, project=project)
    payload = _read(path)
    payload["locale"] = locale
    _write(path, payload)
    return path


def set_lifecycle(
    coordinate: str,
    state: str,
    *,
    scope: PreferenceScope,
    home: Path | None = None,
    project: Path | None = None,
) -> Path:
    if not coordinate or "@" in coordinate or "#" in coordinate:
        raise _input(
            "invalid_coordinate",
            "Lifecycle settings require a capability coordinate, not a revision.",
        )
    if state not in _STATES:
        raise _input(
            "invalid_lifecycle_state",
            "Lifecycle state must be enabled, disabled, or quarantined.",
        )
    path = _scope_path(scope, home=home, project=project)
    payload = _read(path)
    capabilities = payload.get("capabilities")
    values = dict(capabilities) if isinstance(capabilities, dict) else {}
    values[coordinate] = {"state": state}
    payload["capabilities"] = values
    _write(path, payload)
    return path


def lifecycle_states(
    *, home: Path | None = None, project: Path | None = None
) -> dict[str, LifecycleState]:
    payload = resolved_preferences(home=home, project=project)
    return cast(dict[str, LifecycleState], payload["capabilities"])


def _scope_path(scope: PreferenceScope, *, home: Path | None, project: Path | None) -> Path:
    if scope == "global":
        return global_config_path(home)
    if scope == "project":
        return project_config_path((project or Path.cwd()).resolve())
    raise _input("invalid_preference_scope", "Preference scope must be project or global.")


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"schema_version": 1}
    except OSError as error:
        raise _state_error(
            "config_read_failed", "CapSift config could not be read."
        ) from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise _state_error("config_invalid", "CapSift config is not valid JSON.") from error
    if not isinstance(payload, dict):
        raise _state_error("config_invalid", "CapSift config must be a JSON object.")
    return payload


def _write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, prefix=".config-", suffix=".tmp"
        ) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise _state_error(
            "config_write_failed", "CapSift config could not be saved."
        ) from error


def _locale(payload: Mapping[str, object]) -> Locale | None:
    value = payload.get("locale")
    return cast(Locale, value) if isinstance(value, str) and value in _LOCALES else None


def _states(payload: Mapping[str, object]) -> dict[str, LifecycleState]:
    raw = payload.get("capabilities")
    if not isinstance(raw, dict):
        return {}
    states: dict[str, LifecycleState] = {}
    for coordinate, item in raw.items():
        if not isinstance(coordinate, str) or not isinstance(item, dict):
            continue
        state = item.get("state")
        if isinstance(state, str) and state in _STATES:
            states[coordinate] = cast(LifecycleState, state)
    return states


def _input(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INPUT, safe_message=message)


def _state_error(code: str, message: str) -> CapabilityHubError:
    return CapabilityHubError(code=code, category=ErrorCategory.INTERNAL, safe_message=message)
