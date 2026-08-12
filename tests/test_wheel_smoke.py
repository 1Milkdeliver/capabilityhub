from benchmarks.wheel_smoke import run_offline_wheel_smoke


def test_base_wheel_smoke_is_real_and_offline() -> None:
    result = run_offline_wheel_smoke()
    assert result == {
        "api_execute": "passed",
        "network_calls": 0,
        "rag_retrieve": "passed",
        "schema": "capabilityhub.clean-wheel-smoke.v1",
    }
