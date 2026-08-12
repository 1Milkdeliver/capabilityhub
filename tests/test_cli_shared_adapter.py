from __future__ import annotations

import json
from pathlib import Path

import pytest

from capabilityhub import runtime
from capabilityhub.protocol import AdapterKind, RequestEnvelope
from capabilityhub.service_adapter import CapabilityHubServiceAdapter


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    manifests = project / ".capabilityhub" / "manifests"
    manifests.mkdir(parents=True)
    digest = "sha256:" + "c" * 64
    (manifests / "records.json").write_text(
        json.dumps(
            {
                "apiVersion": "capabilityhub.io/v1alpha1",
                "kind": "Capability",
                "metadata": {
                    "namespace": "demo",
                    "name": "records",
                    "version": "1",
                    "digest": digest,
                },
                "spec": {
                    "type": "api",
                    "summary": "Read records",
                    "provider": "static",
                    "operations": [{"name": "read"}],
                    "sections": {
                        "contract": {"content": "read one record", "tokens": 3}
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return project


def test_cli_runtime_delegates_search_load_execute_to_shared_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    seen: list[RequestEnvelope] = []
    original = CapabilityHubServiceAdapter.dispatch

    def capture(
        adapter: CapabilityHubServiceAdapter, request: RequestEnvelope
    ) -> object:
        seen.append(request)
        return original(adapter, request)

    monkeypatch.setattr(CapabilityHubServiceAdapter, "dispatch", capture)

    searched = runtime.local_search("records", project_root=project)
    results = searched["results"]
    assert isinstance(results, list)
    first = results[0]
    assert isinstance(first, dict)
    revision = first["revision"]
    assert isinstance(revision, str)
    runtime.local_load(revision, operation_names=["read"], project_root=project)
    runtime.local_execute_static(
        revision,
        "read",
        {"id": 7},
        {"name": "demo"},
        project_root=project,
    )

    assert [request.operation for request in seen] == [
        "capability.search",
        "capability.load",
        "capability.load",
        "capability.execute",
    ]
    assert all(request.adapter is AdapterKind.CLI for request in seen)
    assert all(request.stream is False and request.cancel_target is None for request in seen)
    assert seen[2].correlation_id == seen[3].correlation_id
    assert seen[2].request_id != seen[3].request_id
    assert all(request.negotiation.decision.compatible for request in seen)
