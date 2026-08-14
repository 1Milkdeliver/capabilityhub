from __future__ import annotations

import hashlib
import json

from capabilityhub.app_update import RELEASE_API, LocalAppUpdater


def _release(version: str, wheel: bytes) -> dict[str, object]:
    name = f"capsift-{version}-py3-none-any.whl"
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/1Milkdeliver/capsift/releases/tag/v{version}",
        "assets": [
            {
                "name": name,
                "size": len(wheel),
                "digest": f"sha256:{hashlib.sha256(wheel).hexdigest()}",
                "browser_download_url": (
                    "https://github.com/1Milkdeliver/capsift/releases/download/"
                    f"v{version}/{name}"
                ),
            }
        ],
    }


def test_check_is_rate_limited_and_uses_no_model_tokens(tmp_path) -> None:
    calls: list[str] = []
    release = json.dumps(_release("0.3.0", b"wheel")).encode()

    def read(url: str, _accept: str, _limit: int) -> bytes:
        calls.append(url)
        return release

    updater = LocalAppUpdater(
        tmp_path, current_version="0.2.0", clock=lambda: 1_000_000, reader=read
    )
    first = updater.check()
    second = updater.check()

    assert first["status"] == "update_available"
    assert first["conversation_tokens"] == 0
    assert second["cached"] is True
    assert second["network_requests"] == 0
    assert calls == [RELEASE_API]


def test_auto_download_stages_only_digest_verified_wheel(tmp_path) -> None:
    wheel = b"verified-wheel"
    release = _release("0.3.0", wheel)

    def read(url: str, _accept: str, _limit: int) -> bytes:
        return json.dumps(release).encode() if url == RELEASE_API else wheel

    result = LocalAppUpdater(tmp_path, current_version="0.2.0", reader=read).check(
        auto_download=True
    )

    assert result["status"] == "downloaded"
    assert result["network_requests"] == 2
    assert result["model_calls"] == 0
    assert (tmp_path / "0.3.0" / "capsift-0.3.0-py3-none-any.whl").read_bytes() == wheel


def test_digest_mismatch_fails_closed_and_does_not_stage(tmp_path) -> None:
    release = _release("0.3.0", b"expected")

    def read(url: str, _accept: str, _limit: int) -> bytes:
        return json.dumps(release).encode() if url == RELEASE_API else b"tampered"

    result = LocalAppUpdater(tmp_path, current_version="0.2.0", reader=read).check(
        auto_download=True
    )

    assert result["status"] == "check_failed"
    assert not (tmp_path / "0.3.0").exists()


def test_older_release_is_up_to_date_and_never_downloaded(tmp_path) -> None:
    release = json.dumps(_release("0.1.0", b"old")).encode()
    calls = 0

    def read(_url: str, _accept: str, _limit: int) -> bytes:
        nonlocal calls
        calls += 1
        return release

    result = LocalAppUpdater(tmp_path, current_version="0.2.0", reader=read).check(
        auto_download=True
    )

    assert result["status"] == "up_to_date"
    assert calls == 1


def test_untrusted_or_unsigned_release_is_not_downloaded(tmp_path) -> None:
    release = _release("0.3.0", b"wheel")
    asset = release["assets"][0]  # type: ignore[index]
    asset["browser_download_url"] = "https://example.com/capsift.whl"  # type: ignore[index]

    result = LocalAppUpdater(
        tmp_path,
        current_version="0.2.0",
        reader=lambda *_args: json.dumps(release).encode(),
    ).check(auto_download=True)

    assert result["status"] == "check_failed"
    assert result["safe_message"].startswith("The update service")


def test_automatic_check_can_be_disabled_without_network(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CAPSIFT_AUTO_UPDATE", "off")

    def unexpected(*_args):
        raise AssertionError("network must not be used")

    result = LocalAppUpdater(tmp_path, reader=unexpected).check(automatic=True)

    assert result["status"] == "disabled"
    assert result["network_requests"] == 0
