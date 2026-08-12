from __future__ import annotations

import json
import urllib.request

from capabilityhub.cli import main
from capabilityhub.protocol import AdapterKind
from capabilityhub.runtime import local_context, local_dashboard


def test_cli_real_path_observes_pending_until_positive_ack(tmp_path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()

    assert (
        main(
            [
                "context",
                "request-removal",
                "resident-handle",
                "--generation",
                "0",
                "--idempotency-key",
                "request-one",
                "--project-root",
                str(project),
            ]
        )
        == 0
    )
    requested = json.loads(capsys.readouterr().out)
    instruction = requested["instruction"]
    assert instruction["confirmed"] is False
    assert requested["pending"] == 1

    assert (
        main(
            [
                "context",
                "ack-removal",
                instruction["instruction_id"],
                "--generation",
                "1",
                "--acknowledgement-id",
                "client-ack",
                "--removed",
                "yes",
                "--project-root",
                str(project),
            ]
        )
        == 0
    )
    acknowledged = json.loads(capsys.readouterr().out)
    assert acknowledged["instruction"]["confirmed"] is True
    assert acknowledged["confirmed"] == 1

    assert main(["context", "removals", "--project-root", str(project)]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["instructions"][0]["confirmed"] is True


def test_dashboard_real_status_reports_http_removal_as_unsupported(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    server = local_dashboard(project)
    try:
        with urllib.request.urlopen(f"{server.url}/api/status", timeout=10) as response:
            status = json.loads(response.read())
    finally:
        server.close()

    negotiation = status["context"]["removal_contract"]["negotiation"]
    assert negotiation == {
        "adapter": AdapterKind.HTTP.value,
        "feature": "context.removal-ack-v1",
        "reason_code": "context_removal_unsupported",
        "supported": False,
    }


def test_library_context_view_does_not_inherit_cli_support(tmp_path) -> None:
    view = local_context(tmp_path, adapter=AdapterKind.LIBRARY)

    assert view["removal_contract"]["negotiation"]["supported"] is False
    assert view["removal_contract"]["confirmed"] == 0
