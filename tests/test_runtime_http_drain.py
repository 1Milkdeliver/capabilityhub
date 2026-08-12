from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from capabilityhub.http_control import HttpControlAccess
from capabilityhub.local_runtime import LocalCatalogGeneration
from capabilityhub.models import (
    CapabilityIdentity,
    CapabilityKind,
    CapabilityManifest,
    ExecutionRequest,
    ExecutionResult,
    OperationSpec,
    OperationType,
)
from capabilityhub.protocol import protocol_handshake
from capabilityhub.registry import CapabilityRegistry
from capabilityhub.runtime import local_http_control
from capabilityhub.update_store import SQLiteUpdateStore


class _VersionedProvider:
    name = "versioned"

    def __init__(self, old_revision: str) -> None:
        self.old_revision = old_revision
        self.old_started = Event()
        self.old_cancelled = Event()
        self.release_old = Event()
        self._calls: list[str] = []
        self._lock = Lock()

    @property
    def calls(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._calls)

    def discover(self) -> tuple[CapabilityManifest, ...]:
        return ()

    def execute(
        self,
        identity: CapabilityIdentity,
        request: ExecutionRequest,
        _context: object,
    ) -> ExecutionResult:
        with self._lock:
            self._calls.append(identity.revision)
        if identity.revision == self.old_revision:
            self.old_started.set()
            if not self.release_old.wait(5):
                raise RuntimeError("old revision test execution was not released")
        return ExecutionResult(
            identity.revision,
            request.operation,
            {"revision": identity.version, "cancelled": self.old_cancelled.is_set()},
            self.name,
            4,
            f"audit-{identity.version}",
        )


class _PointerMonitor:
    def __init__(
        self,
        project: Path,
        manifests: tuple[CapabilityManifest, ...],
        provider: _VersionedProvider,
    ) -> None:
        self.project = project
        self.home = project / "home"
        self._manifests = manifests
        self._provider = provider
        self._store = SQLiteUpdateStore(project / ".capabilityhub" / "state.sqlite3")
        self._generation = 0
        self._last_revision = ""

    def snapshot(self, *, force: bool = False) -> LocalCatalogGeneration:
        del force
        coordinate = self._manifests[0].identity.coordinate
        revision = self._store.state(coordinate).active_revision
        assert revision is not None
        registry = CapabilityRegistry()
        registry.register_many(self._manifests)
        registry.activate(coordinate, revision)
        registry.freeze()
        if revision != self._last_revision:
            self._generation += 1
            self._last_revision = revision
        return LocalCatalogGeneration(
            registry,
            (self._provider,),
            {
                "active_by_kind": {"api": 1},
                "active_total": 1,
                "generation": self._generation,
            },
            0.0,
            60.0,
        )


def _manifest(version: str, digest_character: str) -> CapabilityManifest:
    return CapabilityManifest(
        identity=CapabilityIdentity(
            "test",
            "updatable-http",
            version,
            "sha256:" + digest_character * 64,
        ),
        kind=CapabilityKind.API,
        summary=f"Updatable HTTP revision {version}",
        provider="versioned",
        operations=(OperationSpec("run", OperationType.EXECUTE),),
    )


def _post(
    access: HttpControlAccess,
    operation: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, object]]:
    handshake = protocol_handshake()
    body = json.dumps(
        {
            "request_id": f"request-{operation}-{payload['task_id']}",
            "correlation_id": f"correlation-{operation}-{payload['task_id']}",
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
        return response.status, cast(dict[str, object], json.loads(response.read()))


def _load_execution(access: HttpControlAccess, task: str) -> tuple[str, str]:
    status, searched = _post(
        access,
        "capability.search",
        {"query": "updatable HTTP", "task_id": task},
    )
    assert status == 200
    search_result = cast(dict[str, object], searched["result"])
    card = cast(list[dict[str, object]], search_result["cards"])[0]
    status, loaded = _post(
        access,
        "capability.load",
        {
            "capability_ref": card["capability_ref"],
            "task_id": task,
            "operation_names": ["run"],
        },
    )
    assert status == 200
    loaded_result = cast(dict[str, object], loaded["result"])
    return cast(str, card["revision"]), cast(str, loaded_result["execution_ref"])


def test_staged_pointer_switch_drains_old_http_execution_and_rollback_routes_back(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = SQLiteUpdateStore(project / ".capabilityhub" / "state.sqlite3")
    first = _manifest("1.0.0", "1")
    second = _manifest("2.0.0", "2")
    coordinate = first.identity.coordinate
    store.bootstrap_active(coordinate, first.identity.revision)
    provider = _VersionedProvider(first.identity.revision)
    monitor = _PointerMonitor(project, (first, second), provider)

    def cancel_old(_pin_id: str) -> bool:
        provider.old_cancelled.set()
        provider.release_old.set()
        return True

    control, access = local_http_control(
        monitor=cast(object, monitor),  # type: ignore[arg-type]
        drain_timeout_seconds=0.05,
        execution_cancellable=lambda _manifest, _operation: True,
        cancel_execution=cancel_old,
    )
    try:
        old_revision, old_ref = _load_execution(access, "old-task")
        assert old_revision == first.identity.revision
        with ThreadPoolExecutor(max_workers=1) as pool:
            old_running = pool.submit(
                _post,
                access,
                "capability.execute",
                {
                    "execution_ref": old_ref,
                    "operation": "run",
                    "arguments": {},
                    "task_id": "old-task",
                },
            )
            assert provider.old_started.wait(2)
            pins = store.pins(coordinate)
            assert len(pins) == 1
            assert pins[0].revision == first.identity.revision

            store.stage(
                coordinate,
                second.identity.revision,
                expected_active_revision=first.identity.revision,
            )
            store.record_health(coordinate, second.identity.revision, passed=True)
            store.activate(
                coordinate,
                second.identity.revision,
                expected_active_revision=first.identity.revision,
                validate=lambda _pointers: None,
            )
            new_revision, new_ref = _load_execution(access, "new-task")
            assert new_revision == second.identity.revision
            new_status, new_result = _post(
                access,
                "capability.execute",
                {
                    "execution_ref": new_ref,
                    "operation": "run",
                    "arguments": {},
                    "task_id": "new-task",
                },
            )
            assert new_status == 200
            new_output = cast(dict[str, object], new_result["result"])
            assert new_output["capability_revision"] == second.identity.revision
            old_status, old_result = old_running.result(timeout=3)

        assert old_status == 200, old_result
        old_output = cast(dict[str, object], old_result["result"])
        assert old_output["capability_revision"] == first.identity.revision
        assert provider.old_cancelled.is_set()
        assert store.pins(coordinate) == ()

        store.rollback(
            coordinate,
            expected_active_revision=second.identity.revision,
            validate=lambda _pointers: None,
        )
        rolled_revision, _ = _load_execution(access, "rollback-task")
        assert rolled_revision == first.identity.revision
    finally:
        provider.release_old.set()
        control.close()
