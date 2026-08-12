from benchmarks.adversarial_gate import run_adversarial_gate


def test_adversarial_release_gate_uses_real_local_boundaries() -> None:
    report = run_adversarial_gate()

    assert report.release_ready
    assert report.external_credentials_used is False
    assert {item.case_id for item in report.cases} == {
        "tampered-reference",
        "cross-principal-reference",
        "oversize-provider-output",
        "policy-disconnect-fails-closed",
    }
    assert all(item.actual_code == item.expected_code for item in report.cases)
