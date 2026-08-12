from __future__ import annotations

import tomllib
from pathlib import Path
from types import new_class

import pytest

from capabilityhub.secret_broker import (
    KeyringSecretEnvelope,
    KeyringSecretStore,
    SecretBrokerError,
    resolve_worker_alias,
    worker_secret_scope,
)


class _FakeKeyring:
    def __init__(self, backend_module: str, *, priority: float = 1) -> None:
        backend_type = new_class("Keyring")
        backend_type.__module__ = backend_module
        self.backend = backend_type()
        self.backend.priority = priority
        self.values: dict[tuple[str, str], str] = {}

    def get_keyring(self) -> object:
        return self.backend

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))


@pytest.mark.parametrize(
    ("platform", "backend_module"),
    [
        ("darwin", "keyring.backends.macOS"),
        ("linux", "keyring.backends.SecretService"),
    ],
)
def test_admitted_system_keyring_roundtrip_hides_alias(
    platform: str, backend_module: str
) -> None:
    api = _FakeKeyring(backend_module)
    store = KeyringSecretStore(platform=platform, keyring_api=api)  # type: ignore[arg-type]

    digest = store.put("PRIVATE_TOKEN", "canary-value")

    assert store.get("PRIVATE_TOKEN") == "canary-value"
    assert digest.startswith("sha256:")
    assert all("PRIVATE_TOKEN" not in username for _service, username in api.values)


@pytest.mark.parametrize(
    ("platform", "backend_module"),
    [
        ("linux", "keyring.backends.fail"),
        ("linux", "keyrings.alt.file"),
        ("darwin", "keyring.backends.SecretService"),
    ],
)
def test_headless_insecure_or_wrong_platform_backend_fails_closed(
    platform: str, backend_module: str
) -> None:
    with pytest.raises(SecretBrokerError) as raised:
        KeyringSecretStore(
            platform=platform,
            keyring_api=_FakeKeyring(backend_module),  # type: ignore[arg-type]
        )

    assert raised.value.code == "secret_store_unsupported"


def test_keyring_worker_envelope_retrieves_only_inside_worker_scope() -> None:
    api = _FakeKeyring("keyring.backends.SecretService")
    store = KeyringSecretStore(platform="linux", keyring_api=api)  # type: ignore[arg-type]
    envelope = KeyringSecretEnvelope.seal(
        {"PRIVATE_TOKEN": "worker-canary"}, store=store, platform="linux"
    )

    assert "worker-canary" not in repr(envelope)
    assert "PRIVATE_TOKEN" not in repr(envelope)
    assert resolve_worker_alias("PRIVATE_TOKEN") is None
    with worker_secret_scope(envelope, store=store):
        assert resolve_worker_alias("PRIVATE_TOKEN") == "worker-canary"
    assert resolve_worker_alias("PRIVATE_TOKEN") is None
    with pytest.raises(SecretBrokerError) as replay:
        envelope.open(store=store)
    assert replay.value.code == "secret_transport_consumed"


def test_base_install_does_not_require_keyring() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert all("keyring" not in dependency for dependency in project["project"]["dependencies"])
    assert project["project"]["optional-dependencies"]["secret-store"] == [
        "keyring>=25,<27"
    ]


def test_missing_keyring_dependency_fails_closed() -> None:
    try:
        KeyringSecretStore(platform="linux")
    except SecretBrokerError as error:
        assert error.code == "secret_store_dependency_missing"
    else:
        pytest.skip("keyring is installed in this environment")
