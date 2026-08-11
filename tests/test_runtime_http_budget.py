from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from capabilityhub.hierarchical_budget import (
    DurableHierarchicalBudgetProvider,
    SQLiteHierarchicalBudgetStore,
    load_or_create_hmac_key,
)
from capabilityhub.http_control import HttpControlAccess
from capabilityhub.local_runtime import LocalCatalogMonitor
from capabilityhub.protocol import protocol_handshake
from capabilityhub.runtime import (
    DEFAULT_LOCAL_BUDGETS,
    DEFAULT_LOCAL_HTTP_AGGREGATE_BUDGETS,
    local_http_control,
)


def _configured_cli(project: Path) -> LocalCatalogMonitor:
    root = project / ".capabilityhub" / "manifests"
    root.mkdir(parents=True)
    document = {
        "apiVersion": "capabilityhub.io/v1alpha1",
        "kind": "Capability",
        "metadata": {
            "namespace": "budget-test",
            "name": "configured-budget-cli",
            "version": "1.0.0",
            "digest": "sha256:" + ("d" * 64),
        },
        "spec": {
            "type": "cli",
            "summary": "Configured budget integration fixture.",
            "driver": {
                "name": "cli-process",
                "config": {
                    "executable": sys.executable,
                    "operations": {
                        "run": {
                            "argv": ["-c", "import json; print(json.dumps({'ok': True}))"],
                            "output": "json",
                        }
                    },
                },
            },
            "operations": [{"name": "run", "operationType": "execute"}],
            "sections": {"contract": {"content": "run contract", "tokens": 3}},
        },
    }
    (root / "cli.json").write_text(json.dumps(document), encoding="utf-8")
    return LocalCatalogMonitor(home=project / "home", project=project)


def _skill_monitor(project: Path) -> LocalCatalogMonitor:
    home = project / "home"
    skill = home / ".codex" / "skills" / "budget-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: budget-skill\ndescription: Budget fixture\n---\nbody")
    return LocalCatalogMonitor(home=home, project=project)


def _post(
    access: HttpControlAccess,
    operation: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, object]]:
    handshake = protocol_handshake()
    body = json.dumps(
        {
            "request_id": f"request-{operation}",
            "correlation_id": f"correlation-{operation}",
            "operation": operation,
            "payload": payload,
            "handshake": {
                "api_versions": list(handshake.api_versions),
                "supported_features": list(handshake.supported_features),
                "required_features": list(handshake.required_features),
            },
        }
    ).encode()
    request = Request(
        access.url,
        data=body,
        headers={
            "Authorization": f"Bearer {access.bearer_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def test_local_http_real_three_tool_path_is_hierarchically_budgeted_and_private(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monitor = _configured_cli(project)
    private = {
        "tenant_id": "sensitive-tenant-8472",
        "principal_id": "sensitive-principal-8472",
        "session_id": "sensitive-session-8472",
    }
    task_id = "sensitive-task-8472"
    server, access = local_http_control(
        monitor=monitor,
        task_budget_limits={"loads": 1, "executions": 1},
        **private,
    )
    try:
        search_status, search = _post(
            access,
            "capability.search",
            {"query": "configured budget", "task_id": task_id, "kinds": ["cli"]},
        )
        search_result = search["result"]
        assert isinstance(search_result, dict)
        cards = search_result["cards"]
        assert isinstance(cards, list)
        capability_ref = cards[0]["capability_ref"]
        load_payload = {
            "capability_ref": capability_ref,
            "task_id": task_id,
            "section_names": ["contract"],
            "operation_names": ["run"],
        }
        load_status, loaded = _post(access, "capability.load", load_payload)
        loaded_result = loaded["result"]
        assert isinstance(loaded_result, dict)
        execute_payload = {
            "execution_ref": loaded_result["execution_ref"],
            "operation": "run",
            "arguments": {},
            "task_id": task_id,
        }
        execute_status, executed = _post(access, "capability.execute", execute_payload)
        second_load_status, second_load = _post(access, "capability.load", load_payload)
        second_execute_status, second_execute = _post(
            access,
            "capability.execute",
            execute_payload,
        )
    finally:
        server.close()

    assert (search_status, load_status, execute_status) == (200, 200, 200)
    assert executed["result"]["output"] == {"ok": True}  # type: ignore[index]
    assert (second_load_status, second_execute_status) == (400, 400)
    assert second_load["error"]["code"] == "budget_exhausted"  # type: ignore[index]
    assert second_execute["error"]["code"] == "budget_exhausted"  # type: ignore[index]
    assert "sensitive-" not in json.dumps((second_load, second_execute))

    key = load_or_create_hmac_key(project / ".capabilityhub" / "budget-hmac.key")
    provider = DurableHierarchicalBudgetProvider(
        SQLiteHierarchicalBudgetStore(project / ".capabilityhub" / "state.sqlite3", hmac_key=key),
        tenant_scope=private["tenant_id"],
        principal_scope=private["principal_id"],
        session_scope=private["session_id"],
        aggregate_limits=DEFAULT_LOCAL_HTTP_AGGREGATE_BUDGETS,
        task_limits={**DEFAULT_LOCAL_BUDGETS, "loads": 1, "executions": 1},
    )
    task = provider(task_id)
    assert task.snapshot().used["loads"] == 1
    assert task.snapshot().used["executions"] == 1
    assert task.snapshot().used["bytes"] > 0
    with sqlite3.connect(project / ".capabilityhub" / "state.sqlite3") as connection:
        scope_dump = repr(
            list(
                connection.execute(
                    "SELECT scope_id, root_id, parent_scope_id FROM hierarchical_budget_scopes"
                )
            )
        )
    assert all(value not in scope_dump for value in (*private.values(), task_id))


def test_local_http_budget_survives_restart(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monitor = _skill_monitor(project)
    payload = {"query": "budget-skill", "task_id": "restart-task"}

    first, first_access = local_http_control(monitor=monitor)
    try:
        first_status, first_result = _post(first_access, "capability.search", payload)
    finally:
        first.close()
    second, second_access = local_http_control(monitor=monitor)
    try:
        second_status, _ = _post(second_access, "capability.search", payload)
    finally:
        second.close()

    assert (first_status, second_status) == (200, 200)
    portable_tokens = first_result["result"]["portable_tokens"]  # type: ignore[index]
    key = load_or_create_hmac_key(project / ".capabilityhub" / "budget-hmac.key")
    provider = DurableHierarchicalBudgetProvider(
        SQLiteHierarchicalBudgetStore(project / ".capabilityhub" / "state.sqlite3", hmac_key=key),
        tenant_scope="local",
        principal_scope="operator",
        session_scope="http",
        aggregate_limits=DEFAULT_LOCAL_HTTP_AGGREGATE_BUDGETS,
        task_limits=DEFAULT_LOCAL_BUDGETS,
    )
    assert provider("restart-task").snapshot().used["portable_tokens"] == portable_tokens * 2


def test_two_http_process_boundaries_share_atomic_task_cap(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monitor = _skill_monitor(project)
    probe, probe_access = local_http_control(monitor=monitor)
    try:
        probe_status, probe_result = _post(
            probe_access,
            "capability.search",
            {"query": "budget-skill", "task_id": "probe-task"},
        )
    finally:
        probe.close()
    assert probe_status == 200
    payload_bytes = probe_result["result"]["payload_bytes"]  # type: ignore[index]
    assert isinstance(payload_bytes, int)

    first, first_access = local_http_control(
        monitor=monitor,
        task_budget_limits={"bytes": payload_bytes},
    )
    second, second_access = local_http_control(
        monitor=monitor,
        task_budget_limits={"bytes": payload_bytes},
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda access: _post(
                        access,
                        "capability.search",
                        {"query": "budget-skill", "task_id": "race-task"},
                    ),
                    (first_access, second_access),
                )
            )
    finally:
        first.close()
        second.close()

    assert sorted(status for status, _ in results) == [200, 400]
    failure = next(response for status, response in results if status == 400)
    assert failure["error"]["code"] == "budget_exhausted"  # type: ignore[index]
    assert "race-task" not in json.dumps(failure)
