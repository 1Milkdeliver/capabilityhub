"""Tiny, rate-limited updater for the local CapSift application."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

from capabilityhub import __version__

UPDATE_SCHEMA = "capsift.app-update.v1"
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
MAX_RELEASE_BYTES = 1_000_000
MAX_WHEEL_BYTES = 64 * 1024 * 1024
RELEASE_API = "https://api.github.com/repos/1Milkdeliver/capsift/releases/latest"
_VERSION = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$")
_WHEEL = re.compile(r"^[A-Za-z0-9_.+-]+\.whl$")

UrlReader = Callable[[str, str, int], bytes]


def default_update_root() -> Path:
    """Return the small per-user update cache without touching the project."""

    override = os.environ.get("CAPSIFT_UPDATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data).resolve() / "CapSift" / "updates"
    return Path.home().resolve() / ".local" / "share" / "capsift" / "updates"


class LocalAppUpdater:
    """Check GitHub at most once per interval and stage only digest-verified wheels."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        current_version: str = __version__,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
        reader: UrlReader | None = None,
    ) -> None:
        if interval_seconds < 60:
            raise ValueError("interval_seconds must be at least 60")
        if _parse_version(current_version) is None:
            raise ValueError("current_version must be stable semantic version X.Y.Z")
        self.root = Path(root).resolve() if root is not None else default_update_root()
        self.current_version = current_version
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._reader = reader or _read_url

    def check(
        self,
        *,
        force: bool = False,
        auto_download: bool = False,
        automatic: bool = False,
    ) -> dict[str, Any]:
        """Return a bounded status; network and model failures never break CapSift."""

        if automatic and not _automatic_enabled():
            return self._base("disabled", checked_at=None, network_requests=0)
        now = int(self._clock())
        cached = self._load_state()
        checked_at = cached.get("checked_at")
        if (
            not force
            and isinstance(checked_at, int)
            and 0 <= now - checked_at < self.interval_seconds
        ):
            cached["cached"] = True
            cached["network_requests"] = 0
            return cached
        try:
            release = json.loads(
                self._reader(RELEASE_API, "application/vnd.github+json", MAX_RELEASE_BYTES)
            )
            result = self._evaluate_release(release, now=now, auto_download=auto_download)
        except Exception:
            result = self._base("check_failed", checked_at=now, network_requests=1)
            result["safe_message"] = (
                "The update service could not be reached. CapSift is unchanged."
            )
        self._save_state(result)
        return result

    def _evaluate_release(
        self, release: object, *, now: int, auto_download: bool
    ) -> dict[str, Any]:
        if not isinstance(release, Mapping):
            raise ValueError("invalid release response")
        tag = release.get("tag_name")
        latest = _parse_version(tag)
        current = _parse_version(self.current_version)
        if (
            latest is None
            or current is None
            or release.get("draft") is not False
            or release.get("prerelease") is not False
        ):
            raise ValueError("release is not a stable version")
        latest_text = ".".join(str(item) for item in latest)
        result = self._base("up_to_date", checked_at=now, network_requests=1)
        result["latest_version"] = latest_text
        result["release_url"] = _release_page(release.get("html_url"), tag)
        if latest <= current:
            return result
        asset = _verified_wheel_asset(release.get("assets"), tag, latest_text)
        result["status"] = "update_available"
        result["asset_name"] = asset["name"]
        result["asset_bytes"] = asset["size"]
        result["asset_sha256"] = asset["sha256"]
        if auto_download:
            wheel = self._reader(asset["url"], "application/octet-stream", MAX_WHEEL_BYTES)
            digest_matches = hashlib.sha256(wheel).hexdigest() == asset["sha256"]
            if len(wheel) != asset["size"] or not digest_matches:
                raise ValueError("download digest mismatch")
            destination = self.root / latest_text / asset["name"]
            _atomic_bytes(destination, wheel)
            result["status"] = "downloaded"
            result["staged_path"] = str(destination)
            result["network_requests"] = 2
        return result

    def _base(
        self, status: str, *, checked_at: int | None, network_requests: int
    ) -> dict[str, Any]:
        return {
            "schema": UPDATE_SCHEMA,
            "status": status,
            "current_version": self.current_version,
            "latest_version": None,
            "checked_at": checked_at,
            "next_check_at": (
                None if checked_at is None else checked_at + self.interval_seconds
            ),
            "cached": False,
            "network_requests": network_requests,
            "model_calls": 0,
            "conversation_tokens": 0,
            "auto_download": False,
        }

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(value, dict) or value.get("schema") != UPDATE_SCHEMA:
            return {}
        return value

    def _save_state(self, state: Mapping[str, Any]) -> None:
        try:
            payload = json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
            _atomic_bytes(self.root / "state.json", payload)
        except OSError:
            return


def _automatic_enabled() -> bool:
    return os.environ.get("CAPSIFT_AUTO_UPDATE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _parse_version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _VERSION.fullmatch(value)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _release_page(value: object, tag: object) -> str:
    expected = f"https://github.com/1Milkdeliver/capsift/releases/tag/{tag}"
    if value != expected:
        raise ValueError("unexpected release page")
    return expected


def _verified_wheel_asset(assets: object, tag: object, version: str) -> dict[str, Any]:
    if not isinstance(assets, list):
        raise ValueError("release assets missing")
    candidates: list[dict[str, Any]] = []
    for raw in assets:
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name")
        size = raw.get("size")
        digest = raw.get("digest")
        url = raw.get("browser_download_url")
        match = _SHA256.fullmatch(digest) if isinstance(digest, str) else None
        if (
            isinstance(name, str)
            and _WHEEL.fullmatch(name)
            and version in name
            and isinstance(size, int)
            and 0 < size <= MAX_WHEEL_BYTES
            and match is not None
            and _trusted_asset_url(url, tag, name)
        ):
            candidates.append(
                {"name": name, "size": size, "sha256": match.group(1), "url": url}
            )
    if len(candidates) != 1:
        raise ValueError("release must contain exactly one verified wheel")
    return candidates[0]


def _trusted_asset_url(value: object, tag: object, name: str) -> bool:
    if not isinstance(value, str) or not isinstance(tag, str):
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.query == ""
        and parsed.fragment == ""
        and unquote(parsed.path)
        == f"/1Milkdeliver/capsift/releases/download/{tag}/{name}"
    )


def _read_url(url: str, accept: str, max_bytes: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Accept": accept, "User-Agent": f"CapSift/{__version__} local-updater"},
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        body = cast(bytes, response.read(max_bytes + 1))
    if len(body) > max_bytes:
        raise ValueError("update response too large")
    return body


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise
