from __future__ import annotations

import pytest

from capabilityhub.references import ExpiredReference, InvalidReference, ReferenceSigner


def test_reference_round_trip_authenticates_revision_scope_and_purpose() -> None:
    signer = ReferenceSigner(b"test-signing-key", clock=lambda: 1_000)
    reference = signer.issue(
        revision="community/search@1.2#sha256:abc",
        scope="tenant:user:session",
        purpose="execute",
        ttl_seconds=30,
    )

    claims = signer.verify(
        reference,
        expected_scope="tenant:user:session",
        expected_revision="community/search@1.2#sha256:abc",
        expected_purpose="execute",
    )
    assert claims.expires_at == 1_030
    assert claims.revision.endswith("sha256:abc")


def test_reference_rejects_payload_tampering_and_a_different_key() -> None:
    signer = ReferenceSigner(b"first-key", clock=lambda: 100)
    reference = signer.issue(revision="a/b@1#digest", scope="scope", ttl_seconds=10)
    prefix, payload, signature = reference.split(".")
    replacement = "A" if payload[2] != "A" else "B"
    tampered = f"{prefix}.{payload[:2]}{replacement}{payload[3:]}.{signature}"

    with pytest.raises(InvalidReference) as changed:
        signer.verify(tampered, expected_scope="scope")
    assert changed.value.code == "reference_tampered"

    with pytest.raises(InvalidReference):
        ReferenceSigner(b"different-key", clock=lambda: 100).verify(
            reference, expected_scope="scope"
        )


def test_reference_expiry_is_enforced_at_the_exact_boundary() -> None:
    current = [200.0]
    signer = ReferenceSigner(b"test-key", clock=lambda: current[0])
    reference = signer.issue(revision="a/b@1#digest", scope="scope", ttl_seconds=5)

    current[0] = 204.9
    assert signer.verify(reference, expected_scope="scope").expires_at == 205
    current[0] = 205
    with pytest.raises(ExpiredReference):
        signer.verify(reference, expected_scope="scope")


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"expected_scope": "other"}, "reference_scope_mismatch"),
        (
            {"expected_scope": "scope", "expected_revision": "a/b@2#new"},
            "reference_revision_mismatch",
        ),
        (
            {"expected_scope": "scope", "expected_purpose": "load"},
            "reference_purpose_mismatch",
        ),
    ],
)
def test_reference_rejects_binding_mismatches(kwargs: dict[str, str], code: str) -> None:
    signer = ReferenceSigner(b"test-key", clock=lambda: 100)
    reference = signer.issue(revision="a/b@1#digest", scope="scope", ttl_seconds=10)
    with pytest.raises(InvalidReference) as raised:
        signer.verify(reference, **kwargs)
    assert raised.value.code == code


def test_reference_rejects_malformed_input() -> None:
    signer = ReferenceSigner(b"test-key")
    for value in ("", "plain-text", "chref1.bad.bad", "unknown.a.b"):
        with pytest.raises(InvalidReference):
            signer.verify(value, expected_scope="scope")
