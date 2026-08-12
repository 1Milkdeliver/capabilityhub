from __future__ import annotations

import json

import pytest

from capabilityhub.errors import CapabilityHubError
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.runtime import (
    local_execute,
    local_execute_static,
    local_lifecycle,
    local_load,
    local_search,
    local_set_lifecycle,
)


def _api_monitor(tmp_path) -> tuple[LocalCatalogMonitor, str]:
    project = tmp_path / "project"
    manifests = project / ".capabilityhub" / "manifests"
    manifests.mkdir(parents=True)
    document = {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "namespace": "demo",
            "name": "records",
            "version": "1",
            "digest": "sha256:" + "d" * 64,
        },
        "spec": {
            "type": "api",
            "summary": "Read records",
            "provider": "static",
            "operations": [{"name": "read"}],
            "sections": {"contract": {"content": "read safely", "tokens": 3}},
        },
    }
    (manifests / "records.json").write_text(json.dumps(document), encoding="utf-8")
    return (
        LocalCatalogMonitor(
            home=tmp_path / "home",
            project=project,
            refresh_interval_seconds=0,
        ),
        "demo/records@1#sha256:" + "d" * 64,
    )


def test_stale_last_good_catalog_explicitly_degrades_search(tmp_path, monkeypatch) -> None:
    monitor, _revision = _api_monitor(tmp_path)
    monitor.snapshot()
    monkeypatch.setattr(
        "capabilityhub.local_runtime.local_catalog_fingerprint",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("PRIVATE-ENDPOINT")),
    )

    result = local_search("records", monitor=monitor)

    assert result["total_matches"] == 1
    assert result["dependency"]["outcome"] == "degraded"
    assert result["dependency"]["fallbacks_used"] == [
        "last_good_catalog",
        "last_good_catalog",
    ]
    assert "PRIVATE-ENDPOINT" not in str(result)


def test_load_uses_explicit_manifest_only_fallback_for_missing_provider(tmp_path) -> None:
    monitor, revision = _api_monitor(tmp_path)

    result = local_load(revision, section_names=["contract"], monitor=monitor)

    assert result["sections"][0]["content"] == "read safely"
    assert result["dependency"]["outcome"] == "degraded"
    assert result["dependency"]["fallbacks_used"] == ["manifest_only_load"]


def test_execute_without_real_provider_evidence_fails_closed(tmp_path) -> None:
    monitor, revision = _api_monitor(tmp_path)

    with pytest.raises(CapabilityHubError) as denied:
        local_execute(revision, "read", {}, monitor=monitor)

    assert denied.value.code == "dependency_execute_denied"
    assert denied.value.details["reason_codes"] == (
        "dependency.provider.unavailable",
        "fallback.provider.missing",
    )
    assert "project" not in str(denied.value.as_dict()).lower()


def test_static_provider_is_real_evidence_for_execute(tmp_path) -> None:
    monitor, revision = _api_monitor(tmp_path)

    result = local_execute_static(revision, "read", {}, {"ok": True}, monitor=monitor)

    assert result["output"] == {"ok": True}
    assert result["dependency"]["outcome"] == "allow"
    assert result["dependency"]["reasons"] == ["dependencies.fresh"]


def test_lifecycle_denial_does_not_persist_state(tmp_path) -> None:
    monitor, _revision = _api_monitor(tmp_path)

    with pytest.raises(CapabilityHubError) as denied:
        local_set_lifecycle("demo/records", "disabled", monitor=monitor)

    assert denied.value.code == "dependency_lifecycle_denied"
    assert local_lifecycle(monitor=monitor)["entries"] == []
