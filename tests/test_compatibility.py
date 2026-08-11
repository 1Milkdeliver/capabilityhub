from __future__ import annotations

import pytest

from capabilityhub.compatibility import (
    V1ALPHA1,
    FeatureHandshake,
    decide_compatibility,
    v1alpha1_handshake,
)


def test_v1alpha1_handshake_publishes_stable_required_security_semantics() -> None:
    handshake = v1alpha1_handshake()

    assert handshake.api_versions == (V1ALPHA1,)
    assert "security.required-features-fail-closed" in handshake.supported_features
    assert handshake.required_features == ()


def test_shared_version_and_features_produce_clear_decision() -> None:
    server = v1alpha1_handshake(extra_supported=("runtime.streaming",))
    client = FeatureHandshake(
        (V1ALPHA1,),
        (
            "manifest.extension-preservation",
            "runtime.streaming",
            "runtime.future-optional",
        ),
        ("manifest.extension-preservation",),
    )

    decision = decide_compatibility(client, server)

    assert decision.compatible is True
    assert decision.selected_api_version == V1ALPHA1
    assert decision.enabled_features == (
        "manifest.extension-preservation",
        "runtime.streaming",
    )
    assert decision.ignored_client_optional == ("runtime.future-optional",)
    assert decision.reason_codes == ("compatible",)


def test_unknown_required_security_feature_fails_closed() -> None:
    client = FeatureHandshake(
        (V1ALPHA1,),
        ("security.future-unreviewed-permission",),
        ("security.future-unreviewed-permission",),
    )

    decision = decide_compatibility(client, v1alpha1_handshake())

    assert decision.compatible is False
    assert decision.unsupported_client_required == ("security.future-unreviewed-permission",)
    assert decision.reason_codes == ("unsupported_client_required_feature",)


def test_server_required_feature_and_version_mismatch_are_explicit() -> None:
    client = FeatureHandshake(("capabilityhub.io/v0",), ("manifest.legacy",))
    server = v1alpha1_handshake(extra_required=("security.exact-intent-approval",))

    decision = decide_compatibility(client, server)

    assert decision.compatible is False
    assert decision.selected_api_version is None
    assert decision.unsupported_server_required == ("security.exact-intent-approval",)
    assert decision.reason_codes == (
        "no_shared_api_version",
        "unsupported_server_required_feature",
    )


def test_handshake_rejects_duplicate_or_unadvertised_requirements() -> None:
    with pytest.raises(ValueError, match="unique"):
        FeatureHandshake((V1ALPHA1, V1ALPHA1), ())
    with pytest.raises(ValueError, match="advertised"):
        FeatureHandshake((V1ALPHA1,), ("manifest.json",), ("security.required",))
