from __future__ import annotations

import http.client
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from babeldoc.tools.executor.protocol import EventEnvelope
from babeldoc.tools.executor.runner import FakeExecutionRunner
from babeldoc.tools.executor.server import ALLOW_FAKE_RUNNER_ENV
from babeldoc.tools.executor.server import MAX_JSON_BODY_BYTES
from babeldoc.tools.executor.server import READY_PREFIX
from babeldoc.tools.executor.server import ExecutorHandler
from babeldoc.tools.executor.server import ExecutorServer
from babeldoc.tools.executor.server import _configure_workroot
from babeldoc.tools.executor.server import _is_loopback_host
from babeldoc.tools.executor.server import _load_token
from babeldoc.tools.executor.server import _resolve_parent_identity
from babeldoc.tools.executor.state import CursorAheadError
from babeldoc.tools.executor.state import ExecutionNotFoundError
from babeldoc.tools.executor.state import ExecutionStore
from babeldoc.tools.executor.state import ReplayGapError
from babeldoc.tools.executor.workroot import WORKROOT_ENV
from babeldoc.tools.executor.workroot import WORKROOT_READY_FILE

DEFAULT_SERVER_TOKEN = object()


def prepare_private_workroot(path: Path) -> None:
    path.chmod(0o700)
    marker = path / WORKROOT_READY_FILE
    marker.write_text("ready\n", encoding="utf-8")
    marker.chmod(0o600)


class GapDuringStreamStore:
    def replay(self, _execution_id: str, _after_sequence: int) -> list:
        return []

    def stream(self, execution_id: str, _after_sequence: int):
        yield EventEnvelope(
            type="progress",
            execution_id=execution_id,
            sequence=11,
            emitted_at=time.time(),
            payload={"index": 1},
        )
        raise ReplayGapError

    def snapshot(self, execution_id: str) -> dict:
        return {
            "execution_id": execution_id,
            "task_id": "gap-task",
            "status": "running",
            "initial_sequence": 10,
            "first_available_sequence": 15,
            "last_sequence": 16,
            "worker_finished": False,
            "created_at": time.time(),
            "finished_at": None,
        }


class EvictedDuringStreamStore:
    def replay(self, _execution_id: str, _after_sequence: int) -> list:
        return []

    def stream(self, execution_id: str, _after_sequence: int):
        yield EventEnvelope(
            type="progress",
            execution_id=execution_id,
            sequence=11,
            emitted_at=time.time(),
            payload={"index": 1},
        )
        raise ExecutionNotFoundError(execution_id)

    def snapshot(self, execution_id: str) -> dict:
        raise ExecutionNotFoundError(execution_id)


class CursorAheadThenEvictedStore:
    def replay(self, execution_id: str, _after_sequence: int) -> list:
        raise CursorAheadError(execution_id)

    def snapshot(self, execution_id: str) -> dict:
        raise ExecutionNotFoundError(execution_id)


class PausingCreateStore(ExecutionStore):
    def __init__(self) -> None:
        super().__init__(FakeExecutionRunner())
        self.create_entered = threading.Event()
        self.allow_create = threading.Event()

    def create(self, request: dict) -> dict:
        self.create_entered.set()
        if not self.allow_create.wait(timeout=2):
            raise TimeoutError("test did not release create")
        return super().create(request)


class NeverIdleStore(ExecutionStore):
    def __init__(self) -> None:
        super().__init__(FakeExecutionRunner())
        self.drain_wait_started = threading.Event()
        self.release_drain_wait = threading.Event()
        self.wait_timeouts: list[float | None] = []
        self.abort_reasons: list[str] = []

    def abort_current(self, *, reason: str = "client_request") -> None:
        self.abort_reasons.append(reason)
        super().abort_current(reason=reason)

    def wait_until_idle(self, timeout_seconds: float | None = None) -> bool:
        self.wait_timeouts.append(timeout_seconds)
        if timeout_seconds == 5.0:
            return False
        self.drain_wait_started.set()
        self.release_drain_wait.wait(timeout=2)
        return False


class ClearsWorkrootEnvironmentOnBeginStore(ExecutionStore):
    def begin_heavy_operation(self, operation_id: str) -> threading.Event:
        abort_event = super().begin_heavy_operation(operation_id)
        os.environ.pop(WORKROOT_ENV, None)
        return abort_event


@contextmanager
def running_server(
    tmp_path: Path,
    *,
    max_event_log_size: int = 1000,
    execution_store=None,
    workroot: Path | None = None,
    parent_pid: int | None = None,
    parent_start_time: float | None = None,
) -> Iterator[tuple[ExecutorServer, threading.Thread]]:
    previous_workroot = os.environ.get(WORKROOT_ENV)
    os.environ[WORKROOT_ENV] = str(tmp_path)
    store = execution_store or ExecutionStore(
        FakeExecutionRunner(), max_event_log_size=max_event_log_size
    )
    server = ExecutorServer(
        ("127.0.0.1", 0),
        store,
        token=secrets.token_urlsafe(24),
        instance_id="test-instance",
        workroot=workroot,
        parent_pid=parent_pid,
        parent_start_time=parent_start_time,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, thread
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if previous_workroot is None:
            os.environ.pop(WORKROOT_ENV, None)
        else:
            os.environ[WORKROOT_ENV] = previous_workroot


def request(
    server: ExecutorServer,
    method: str,
    path: str,
    *,
    body: object | None = None,
    token: str | None | object = DEFAULT_SERVER_TOKEN,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*server.server_address[:2], timeout=3)
    request_headers = dict(headers or {})
    if token is DEFAULT_SERVER_TOKEN:
        token = server.token
    if isinstance(token, str):
        request_headers["Authorization"] = f"Bearer {token}"
    encoded: bytes | None = None
    if isinstance(body, bytes):
        encoded = body
        request_headers["Content-Type"] = "application/json"
    elif body is not None:
        encoded = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    data = response.read()
    result_headers = {name.lower(): value for name, value in response.getheaders()}
    status = response.status
    connection.close()
    return status, result_headers, data


def json_request(*args, **kwargs) -> tuple[int, dict[str, str], dict]:
    status, headers, body = request(*args, **kwargs)
    return status, headers, json.loads(body)


def test_requires_bearer_token_and_reports_service_identity(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, _thread):
        status, headers, payload = json_request(
            server,
            "GET",
            "/healthz",
            token=None,
        )
        assert status == 401
        assert headers["www-authenticate"] == "Bearer"
        assert payload["code"] == "unauthorized"

        status, _headers, payload = json_request(server, "GET", "/healthz")
        assert status == 200
        assert payload["ok"] is True
        assert payload["service_id"] == "gloss-babeldoc"
        assert payload["instance_id"] == "test-instance"
        assert payload["endpoint"].endswith(f":{server.server_port}")

        status, _headers, payload = json_request(
            server,
            "GET",
            "/healthz",
            token="é" * 40,
        )
        assert status == 401
        assert payload["code"] == "unauthorized"


def test_runtime_endpoint_exposes_protocol_capabilities(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, _thread):
        status, _headers, payload = json_request(server, "GET", "/v1/runtime")

    assert status == 200
    assert payload["service"]["instance_id"] == "test-instance"
    assert "executor.http.v1" in payload["capabilities"]
    assert "executor.events.ndjson.v1" in payload["capabilities"]


def test_create_snapshot_stream_and_idempotent_replay(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, _thread):
        execution_request = {"task_id": "task-1", "mode": "burst"}
        status, _headers, created = json_request(
            server,
            "POST",
            "/v1/executions",
            body=execution_request,
        )
        assert status == 201
        assert created["replayed"] is False
        execution_id = created["execution_id"]

        status, _headers, replayed = json_request(
            server,
            "POST",
            "/v1/executions",
            body=execution_request,
        )
        assert status == 200
        assert replayed["replayed"] is True
        assert replayed["execution_id"] == execution_id

        status, _headers, conflict = json_request(
            server,
            "POST",
            "/v1/executions",
            body={"task_id": "task-1", "mode": "different"},
        )
        assert status == 409
        assert conflict["code"] == "idempotency_conflict"
        assert conflict["snapshot"]["execution_id"] == execution_id

        status, _headers, latest = json_request(
            server,
            "GET",
            "/v1/executions/latest",
        )
        assert status == 200
        assert latest["execution"]["execution_id"] == execution_id

        status, _headers, snapshot = json_request(
            server,
            "GET",
            f"/v1/executions/{execution_id}",
        )
        assert status == 200
        assert snapshot["task_id"] == "task-1"

        status, headers, body = request(
            server,
            "GET",
            f"/v1/executions/{execution_id}/events"
            f"?after_sequence={created['initial_sequence']}",
        )
        events = [json.loads(line) for line in body.splitlines()]
        assert status == 200
        assert headers["content-type"] == "application/x-ndjson"
        assert [event["type"] for event in events] == [
            "progress",
            "progress",
            "result",
        ]
        assert all(event["execution_id"] == execution_id for event in events)
        assert all(event["schema_version"] == 1 for event in events)
        assert all(event["service_id"] == "gloss-babeldoc" for event in events)
        assert all(event["instance_id"] == "test-instance" for event in events)
        assert all(isinstance(event["emitted_at"], float) for event in events)

        status, _headers, current = json_request(
            server,
            "GET",
            "/v1/executions/current",
        )
        assert status == 200
        assert current["execution"] is None

        status, _headers, payload = json_request(
            server,
            "GET",
            f"/v1/executions/{execution_id}/events"
            f"?after_sequence={events[-1]['sequence'] + 1}",
        )
        assert status == 409
        assert payload["code"] == "cursor_ahead"
        assert payload["snapshot"]["execution_id"] == execution_id


def test_replay_gap_includes_authoritative_snapshot(tmp_path: Path) -> None:
    with running_server(tmp_path, max_event_log_size=1) as (server, _thread):
        status, _headers, created = json_request(
            server,
            "POST",
            "/v1/executions",
            body={"task_id": "task-gap", "mode": "burst"},
        )
        assert status == 201
        execution_id = created["execution_id"]

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            _, _, snapshot = json_request(
                server,
                "GET",
                f"/v1/executions/{execution_id}",
            )
            if snapshot["status"] == "succeeded":
                break
            time.sleep(0.01)

        status, _headers, payload = json_request(
            server,
            "GET",
            f"/v1/executions/{execution_id}/events"
            f"?after_sequence={created['initial_sequence']}",
        )

    assert status == 410
    assert payload["code"] == "replay_gap"
    assert payload["snapshot"]["execution_id"] == execution_id
    assert payload["snapshot"]["status"] == "succeeded"


def test_stream_gap_emits_an_unsequenced_control_record(tmp_path: Path) -> None:
    with running_server(
        tmp_path,
        execution_store=GapDuringStreamStore(),
    ) as (server, _thread):
        status, _headers, body = request(
            server,
            "GET",
            "/v1/executions/gap-execution/events?after_sequence=10",
        )

    records = [json.loads(line) for line in body.splitlines()]
    assert status == 200
    assert [record["type"] for record in records] == ["progress", "stream_error"]
    assert records[1]["sequence"] is None
    assert records[1]["payload"]["code"] == "replay_gap"
    assert records[1]["payload"]["after_sequence"] == 11


def test_stream_eviction_emits_control_record_with_nullable_snapshot(
    tmp_path: Path,
) -> None:
    with running_server(
        tmp_path,
        execution_store=EvictedDuringStreamStore(),
    ) as (server, _thread):
        status, _headers, body = request(
            server,
            "GET",
            "/v1/executions/evicted-execution/events?after_sequence=10",
        )

    records = [json.loads(line) for line in body.splitlines()]
    assert status == 200
    assert [record["type"] for record in records] == ["progress", "stream_error"]
    assert records[1]["sequence"] is None
    assert records[1]["payload"]["code"] == "execution_not_found"
    assert records[1]["payload"]["snapshot"] is None


def test_cursor_ahead_snapshot_may_be_evicted_concurrently(tmp_path: Path) -> None:
    with running_server(
        tmp_path,
        execution_store=CursorAheadThenEvictedStore(),
    ) as (server, _thread):
        status, _headers, payload = json_request(
            server,
            "GET",
            "/v1/executions/evicted-execution/events?after_sequence=10",
        )

    assert status == 409
    assert payload["code"] == "cursor_ahead"
    assert payload["snapshot"] is None


def test_targeted_cancel_emits_terminal_event_and_shutdown_stops_server(
    tmp_path: Path,
) -> None:
    with running_server(tmp_path) as (server, thread):
        status, _headers, created = json_request(
            server,
            "POST",
            "/v1/executions",
            body={"task_id": "task-block", "mode": "block"},
        )
        assert status == 201
        execution_id = created["execution_id"]

        status, _headers, current = json_request(
            server,
            "GET",
            "/v1/executions/current",
        )
        assert status == 200
        assert current["execution"]["execution_id"] == execution_id

        status, _headers, cancelling = json_request(
            server,
            "POST",
            f"/v1/executions/{execution_id}/cancel",
            body={},
        )
        assert status == 202
        assert cancelling["execution_id"] == execution_id
        assert cancelling["status"] in {"cancelling", "cancelled"}

        status, _headers, body = request(
            server,
            "GET",
            f"/v1/executions/{execution_id}/events"
            f"?after_sequence={created['initial_sequence']}",
        )
        events = [json.loads(line) for line in body.splitlines()]
        assert status == 200
        assert events[-1]["type"] == "cancelled"
        assert events[-1]["payload"]["reason"] == "client_request"

        status, _headers, payload = json_request(
            server,
            "POST",
            "/v1/shutdown",
            body={},
        )
        assert status == 202
        assert payload["status"] == "stopping"
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_json_body_must_be_an_object_and_is_limited_to_one_mib(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, _thread):
        status, _headers, payload = json_request(
            server,
            "POST",
            "/v1/executions",
            body=["not", "an", "object"],
        )
        assert status == 400
        assert payload["message"] == "json body must be an object"

        status, _headers, payload = json_request(
            server,
            "POST",
            "/v1/executions",
            headers={"Content-Length": str(MAX_JSON_BODY_BYTES + 1)},
        )
        assert status == 413
        assert payload["code"] == "invalid_request"


def test_json_parser_failures_return_a_structured_bad_request(tmp_path: Path) -> None:
    oversized_integer = b'{"value":' + (b"9" * 5000) + b"}"
    deeply_nested = b'{"value":' + (b"[" * 10_000) + b"0" + (b"]" * 10_000) + b"}"
    cases = (
        ("/v1/shutdown", oversized_integer),
        ("/v1/executions/missing/cancel", oversized_integer),
        ("/v1/executions", deeply_nested),
        ("/v1/pdf/watermark1", deeply_nested),
    )

    with running_server(tmp_path) as (server, thread):
        for path, body in cases:
            status, _headers, payload = json_request(
                server,
                "POST",
                path,
                body=body,
            )
            assert status == 400
            assert payload == {"code": "invalid_request", "message": "invalid json"}
        assert thread.is_alive()


def test_stopping_service_rejects_new_work(tmp_path: Path) -> None:
    with running_server(tmp_path) as (server, _thread):
        server._stopping.set()

        status, _headers, payload = json_request(
            server,
            "POST",
            "/v1/executions",
            body={"task_id": "too-late", "mode": "burst"},
        )

        assert status == 503
        assert payload["code"] == "service_stopping"
        assert server.store.current_snapshot() is None

        status, _headers, payload = json_request(
            server,
            "POST",
            "/v1/abort",
            body={},
        )
        assert status == 404
        assert payload["code"] == "not_found"


def test_shutdown_admission_gate_cancels_a_concurrent_create(tmp_path: Path) -> None:
    store = PausingCreateStore()
    with running_server(tmp_path, execution_store=store) as (server, thread):
        created: list[dict] = []
        create_thread = threading.Thread(
            target=lambda: created.append(
                server.admit_execution({"task_id": "racing-task", "mode": "block"})
            )
        )
        create_thread.start()
        assert store.create_entered.wait(timeout=1)

        shutdown_thread = threading.Thread(target=server.request_shutdown)
        shutdown_thread.start()
        time.sleep(0.02)
        assert shutdown_thread.is_alive()

        store.allow_create.set()
        create_thread.join(timeout=2)
        shutdown_thread.join(timeout=2)
        assert created
        assert store.wait_until_idle(timeout_seconds=2)
        snapshot = store.snapshot(created[0]["execution_id"])
        assert snapshot["status"] == "cancelled"
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_shutdown_escalation_bounds_an_existing_graceful_drain(
    tmp_path: Path,
) -> None:
    store = NeverIdleStore()
    with running_server(tmp_path, execution_store=store) as (server, thread):
        assert server.request_shutdown(cancel_active=False) is True
        assert store.drain_wait_started.wait(timeout=1)

        assert server.request_shutdown(cancel_active=True) is False
        store.release_drain_wait.set()

        thread.join(timeout=2)
        assert not thread.is_alive()
        assert store.wait_timeouts[-1] == 5.0


def test_parent_loss_escalates_an_existing_graceful_drain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    parent_alive = threading.Event()
    parent_alive.set()
    monkeypatch.setattr(
        "babeldoc.tools.executor.server.PARENT_WATCHDOG_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "babeldoc.tools.executor.server._process_matches",
        lambda _process_id, _start_time: parent_alive.is_set(),
    )
    store = NeverIdleStore()
    with running_server(
        tmp_path,
        execution_store=store,
        parent_pid=123,
        parent_start_time=456.0,
    ) as (server, thread):
        server.start_parent_watchdog()
        assert server.request_shutdown(cancel_active=False) is True
        assert store.drain_wait_started.wait(timeout=1)

        parent_alive.clear()
        deadline = time.monotonic() + 1
        while "parent_exit" not in store.abort_reasons and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "parent_exit" in store.abort_reasons
        store.release_drain_wait.set()

        thread.join(timeout=2)
        assert not thread.is_alive()
        assert store.wait_timeouts[-1] == 5.0
        assert server._watchdog is not None
        server._watchdog.join(timeout=1)
        assert not server._watchdog.is_alive()


def test_service_rejects_non_loopback_hosts() -> None:
    unspecified_address = ".".join(["0", "0", "0", "0"])
    assert _is_loopback_host("127.0.0.1")
    assert not _is_loopback_host("localhost")
    assert not _is_loopback_host("::1")
    assert not _is_loopback_host(unspecified_address)
    assert not _is_loopback_host("example.com")


def test_token_file_is_used_without_ready_disclosure(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_value = secrets.token_urlsafe(32)
    token_file.write_text(f"{token_value}\n", encoding="utf-8")
    token_file.chmod(0o600)

    assert _load_token(token_file) == (token_value, False)
    generated, should_disclose = _load_token(None)
    assert should_disclose is True
    assert len(generated) >= 32


def test_token_file_fails_closed_when_missing_or_insecure(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("x" * 40, encoding="utf-8")
    token_file.chmod(0o644)

    try:
        _load_token(token_file)
    except ValueError as error:
        assert "permissions" in str(error)
    else:
        raise AssertionError("insecure token file was accepted")

    try:
        _load_token(tmp_path / "missing-token")
    except ValueError as error:
        assert "unable to read" in str(error)
    else:
        raise AssertionError("missing token file generated a replacement token")


def test_configure_workroot_requires_an_existing_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(WORKROOT_ENV, raising=False)
    prepare_private_workroot(tmp_path)
    assert _configure_workroot(tmp_path, required=True) == tmp_path.resolve()
    assert os.environ[WORKROOT_ENV] == str(tmp_path.resolve())

    try:
        _configure_workroot(tmp_path / "missing", required=True)
    except ValueError as error:
        assert "existing directory" in str(error)
    else:
        raise AssertionError("missing workroot was accepted")


def test_configure_workroot_requires_private_marker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private-root"
    private_root.mkdir(mode=0o700)
    monkeypatch.delenv(WORKROOT_ENV, raising=False)

    try:
        _configure_workroot(private_root, required=True)
    except ValueError as error:
        assert "readiness proof" in str(error)
    else:
        raise AssertionError("workroot without readiness proof was accepted")


def test_health_degrades_when_workroot_disappears(tmp_path: Path) -> None:
    workroot = tmp_path / "runtime"
    workroot.mkdir(mode=0o700)
    prepare_private_workroot(workroot)
    store = ExecutionStore(FakeExecutionRunner())
    with running_server(
        tmp_path,
        execution_store=store,
        workroot=workroot,
    ) as (server, _thread):
        moved = tmp_path / "runtime-moved"
        workroot.rename(moved)
        status, _headers, payload = json_request(server, "GET", "/healthz")

    assert status == 503
    assert payload["ok"] is False
    assert payload["code"] == "workroot_unavailable"


def test_watermark_uses_server_workroot_and_finishes_after_begin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "input.pdf"
    asset_file = tmp_path / "watermark.pdf"
    input_file.write_bytes(b"%PDF-1.4\ninput")
    asset_file.write_bytes(b"%PDF-1.4\nasset")
    store = ClearsWorkrootEnvironmentOnBeginStore()

    def write_output(
        _operation: str,
        _input_file: Path,
        output_file: Path,
        _asset_files: list[Path],
        _abort_event,
    ) -> None:
        output_file.write_bytes(b"%PDF-1.4\noutput")

    monkeypatch.setattr(
        ExecutorHandler,
        "_run_watermark_subprocess",
        staticmethod(write_output),
    )
    with running_server(
        tmp_path,
        execution_store=store,
        workroot=tmp_path,
    ) as (server, _thread):
        status, _headers, payload = json_request(
            server,
            "POST",
            "/v1/pdf/watermark1",
            body={
                "operation_id": "watermark-stable-root",
                "input_file": input_file.name,
                "output_file": "output/result.pdf",
                "asset_file": asset_file.name,
            },
        )

    snapshot = store.snapshot("watermark-stable-root")
    assert status == 200
    assert payload["output_file"] == "output/result.pdf"
    assert snapshot["status"] == "succeeded"
    assert snapshot["worker_finished"] is True
    assert (tmp_path / "output/result.pdf").is_file()


def test_parent_identity_is_captured_and_mismatch_is_rejected() -> None:
    parent_pid, parent_start_time = _resolve_parent_identity(os.getpid(), None)
    assert parent_pid == os.getpid()
    assert isinstance(parent_start_time, float)

    try:
        _resolve_parent_identity(os.getpid(), 0)
    except ValueError as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("stale parent identity was accepted")


def test_parent_identity_mismatch_stops_service() -> None:
    server = ExecutorServer(
        ("127.0.0.1", 0),
        ExecutionStore(FakeExecutionRunner()),
        token=secrets.token_urlsafe(24),
        parent_pid=os.getpid(),
        parent_start_time=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        server.start_parent_watchdog()
        thread.join(timeout=3)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gloss_cli_serve_emits_generated_token_in_ready_handshake(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment[WORKROOT_ENV] = str(tmp_path)
    environment[ALLOW_FAKE_RUNNER_ENV] = "1"
    prepare_private_workroot(tmp_path)
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "babeldoc.gloss_cli",
            "serve",
            "--runner",
            "fake",
            "--instance-id",
            "subprocess-instance",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        ready_line = process.stdout.readline().strip()
        assert ready_line.startswith(READY_PREFIX)
        ready = json.loads(ready_line.removeprefix(READY_PREFIX))
        assert ready["instance_id"] == "subprocess-instance"
        assert ready["auth_token"]

        parsed = urlparse(ready["endpoint"])
        connection = http.client.HTTPConnection(
            parsed.hostname,
            parsed.port,
            timeout=3,
        )
        connection.request(
            "POST",
            "/v1/shutdown",
            body=b"{}",
            headers={
                "Authorization": f"Bearer {ready['auth_token']}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 202
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_sigterm_performs_controlled_service_shutdown(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment[WORKROOT_ENV] = str(tmp_path)
    environment[ALLOW_FAKE_RUNNER_ENV] = "1"
    prepare_private_workroot(tmp_path)
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "babeldoc.gloss_cli",
            "serve",
            "--runner",
            "fake",
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        ready_line = process.stdout.readline().strip()
        ready = json.loads(ready_line.removeprefix(READY_PREFIX))
        parsed = urlparse(ready["endpoint"])
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        connection.request(
            "POST",
            "/v1/executions",
            body=json.dumps({"task_id": "signal-task", "mode": "block"}),
            headers={
                "Authorization": f"Bearer {ready['auth_token']}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 201

        process.send_signal(signal.SIGTERM)
        return_code = process.wait(timeout=5)
        stderr = process.stderr.read() if process.stderr is not None else ""
        assert return_code == 0, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
