from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from babeldoc.tools.executor.protocol import WorkerEvent
from babeldoc.tools.executor.runner import ExecutionRunner
from babeldoc.tools.executor.state import CursorAheadError
from babeldoc.tools.executor.state import ExecutionBusyError
from babeldoc.tools.executor.state import ExecutionConflictError
from babeldoc.tools.executor.state import ExecutionNotFoundError
from babeldoc.tools.executor.state import ExecutionStore
from babeldoc.tools.executor.state import ReplayGapError


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


class BlockingRunner(ExecutionRunner):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_seen = threading.Event()
        self._lock = threading.Lock()
        self.invocations = 0

    def run(self, request, emit, abort_event) -> None:
        with self._lock:
            self.invocations += 1
        self.started.set()
        if request.get("mode") == "result_then_block":
            emit(WorkerEvent("result", {"files": {}}))
            self.release.wait(timeout=2)
            return
        if request.get("mode") == "finish":
            emit(WorkerEvent("result", {"files": {}}))
            return
        abort_event.wait(timeout=2)
        if abort_event.is_set():
            self.cancel_seen.set()
        self.release.wait(timeout=2)


class ImmediateEventRunner(ExecutionRunner):
    def run(self, request, emit, abort_event) -> None:
        for index in range(request.get("progress_events", 0)):
            emit(WorkerEvent("progress", {"index": index}))
        event_type = request.get("event_type", "result")
        emit(WorkerEvent(event_type, {"code": event_type, "files": {}}))


def snapshot_is_finished(store: ExecutionStore, execution_id: str) -> bool:
    return bool(store.snapshot(execution_id)["worker_finished"])


def test_create_is_idempotent_for_same_task_and_canonical_request() -> None:
    runner = BlockingRunner()
    store = ExecutionStore(runner)
    request = {
        "task_id": "task-1",
        "mode": "wait",
        "nested": {"b": 2, "a": 1},
    }

    first = store.create(request)
    assert runner.started.wait(timeout=1)
    replay = store.create(
        {
            "nested": {"a": 1, "b": 2},
            "mode": "wait",
            "task_id": "task-1",
        }
    )

    assert first["execution_id"] == replay["execution_id"]
    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert replay["status"] == "running"
    assert runner.invocations == 1

    with pytest.raises(ExecutionConflictError) as conflict:
        store.create({"task_id": "task-1", "mode": "different"})
    assert conflict.value.snapshot["execution_id"] == first["execution_id"]

    store.cancel(first["execution_id"])
    runner.release.set()
    wait_until(lambda: snapshot_is_finished(store, first["execution_id"]))


def test_worker_start_failure_returns_replayable_failed_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ExecutionStore(ImmediateEventRunner())

    def fail_to_start(_record) -> None:
        raise RuntimeError("thread limit reached")

    monkeypatch.setattr(store, "_start_worker", fail_to_start)
    request = {"task_id": "worker-start-failure", "mode": "finish"}

    first = store.create(request)
    snapshot = store.snapshot(first["execution_id"])
    events = store.replay(first["execution_id"], first["initial_sequence"])
    replay = store.create(request)

    assert first["status"] == "failed"
    assert first["replayed"] is False
    assert snapshot["status"] == "failed"
    assert snapshot["worker_finished"] is True
    assert store.active_snapshot() is None
    assert store.wait_until_idle(timeout_seconds=0) is True
    assert [event.type for event in events] == ["error"]
    assert events[0].payload == {
        "code": "worker_start_failed",
        "message": "thread limit reached",
        "message_for_user": None,
        "details": {"exception_type": "RuntimeError"},
    }
    assert replay == {**first, "replayed": True}


def test_terminal_event_does_not_release_single_active_worker_early() -> None:
    runner = BlockingRunner()
    store = ExecutionStore(runner)
    first = store.create({"task_id": "first", "mode": "result_then_block"})
    assert runner.started.wait(timeout=1)
    wait_until(lambda: store.snapshot(first["execution_id"])["status"] == "succeeded")

    snapshot = store.snapshot(first["execution_id"])
    assert snapshot["worker_finished"] is False
    with pytest.raises(ExecutionBusyError) as busy:
        store.create({"task_id": "second", "mode": "finish"})
    assert busy.value.snapshot["execution_id"] == first["execution_id"]

    runner.release.set()
    wait_until(lambda: snapshot_is_finished(store, first["execution_id"]))
    second = store.create({"task_id": "second", "mode": "finish"})
    wait_until(lambda: snapshot_is_finished(store, second["execution_id"]))
    assert store.snapshot(second["execution_id"])["status"] == "succeeded"


def test_targeted_cancel_stays_busy_until_worker_exits_and_emits_terminal() -> None:
    runner = BlockingRunner()
    store = ExecutionStore(runner)
    started = store.create({"task_id": "cancel-me", "mode": "wait"})
    execution_id = started["execution_id"]
    assert runner.started.wait(timeout=1)

    cancelling = store.cancel(execution_id)
    assert cancelling["status"] == "cancelling"
    assert cancelling["worker_finished"] is False
    assert runner.cancel_seen.wait(timeout=1)
    assert store.replay(execution_id, started["initial_sequence"]) == []

    with pytest.raises(ExecutionBusyError):
        store.create({"task_id": "too-early", "mode": "finish"})
    with pytest.raises(ExecutionNotFoundError):
        store.cancel("missing-execution")

    runner.release.set()
    wait_until(lambda: snapshot_is_finished(store, execution_id))
    snapshot = store.snapshot(execution_id)
    events = store.replay(execution_id, started["initial_sequence"])
    assert snapshot["status"] == "cancelled"
    assert snapshot["worker_finished"] is True
    assert [event.type for event in events] == ["cancelled"]
    assert events[0].payload == {
        "reason": "client_request",
        "code": "cancelled",
        "message": "execution cancelled",
        "message_for_user": None,
        "details": {},
    }

    repeated = store.cancel(execution_id)
    assert repeated == snapshot


def test_wait_until_idle_tracks_worker_cleanup() -> None:
    runner = BlockingRunner()
    store = ExecutionStore(runner)
    started = store.create({"task_id": "shutdown", "mode": "wait"})
    assert runner.started.wait(timeout=1)

    store.cancel(started["execution_id"])
    assert store.wait_until_idle(timeout_seconds=0.01) is False
    with pytest.raises(ValueError):
        store.wait_until_idle(timeout_seconds=-1)

    runner.release.set()
    assert store.wait_until_idle(timeout_seconds=1) is True
    assert store.snapshot(started["execution_id"])["worker_finished"] is True


@pytest.mark.parametrize(
    ("event_type", "expected_status"),
    [
        ("result", "succeeded"),
        ("error", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_terminal_events_map_to_stable_statuses(
    event_type: str,
    expected_status: str,
) -> None:
    store = ExecutionStore(ImmediateEventRunner())
    started = store.create({"task_id": event_type, "event_type": event_type})
    wait_until(lambda: snapshot_is_finished(store, started["execution_id"]))

    snapshot = store.snapshot(started["execution_id"])
    events = store.replay(started["execution_id"], started["initial_sequence"])
    assert snapshot["status"] == expected_status
    assert [event.type for event in events] == [event_type]


def test_old_execution_remains_available_after_new_execution() -> None:
    store = ExecutionStore(ImmediateEventRunner())
    first = store.create({"task_id": "first"})
    wait_until(lambda: snapshot_is_finished(store, first["execution_id"]))
    second = store.create({"task_id": "second"})
    wait_until(lambda: snapshot_is_finished(store, second["execution_id"]))

    assert store.snapshot(first["execution_id"])["status"] == "succeeded"
    assert [
        event.type
        for event in store.replay(first["execution_id"], first["initial_sequence"])
    ] == ["result"]
    assert store.current_snapshot() == store.snapshot(second["execution_id"])


def test_store_retains_sixteen_most_recent_executions() -> None:
    store = ExecutionStore(ImmediateEventRunner())
    executions: list[dict[str, Any]] = []
    for index in range(17):
        execution = store.create({"task_id": f"task-{index}"})
        execution_id = execution["execution_id"]
        wait_until(
            lambda execution_id=execution_id: snapshot_is_finished(store, execution_id)
        )
        executions.append(execution)

    with pytest.raises(ExecutionNotFoundError):
        store.snapshot(executions[0]["execution_id"])
    for execution in executions[1:]:
        assert store.snapshot(execution["execution_id"])["status"] == "succeeded"


def test_replay_gap_reports_trimmed_event_history() -> None:
    store = ExecutionStore(ImmediateEventRunner(), max_event_log_size=2)
    started = store.create(
        {"task_id": "bursty", "progress_events": 3, "event_type": "result"}
    )
    execution_id = started["execution_id"]
    wait_until(lambda: snapshot_is_finished(store, execution_id))

    with pytest.raises(ReplayGapError):
        store.replay(execution_id, started["initial_sequence"])

    snapshot = store.snapshot(execution_id)
    first_available = snapshot["first_available_sequence"]
    assert isinstance(first_available, int)
    events = store.replay(execution_id, first_available - 1)
    assert [event.type for event in events] == ["progress", "result"]


def test_future_cursor_is_rejected_and_snapshots_have_timestamps() -> None:
    store = ExecutionStore(ImmediateEventRunner())
    started = store.create({"task_id": "future-cursor"})
    execution_id = started["execution_id"]
    wait_until(lambda: snapshot_is_finished(store, execution_id))

    snapshot = store.snapshot(execution_id)
    assert isinstance(snapshot["created_at"], float)
    assert isinstance(snapshot["finished_at"], float)
    assert snapshot["finished_at"] >= snapshot["created_at"]
    with pytest.raises(CursorAheadError):
        store.replay(execution_id, snapshot["last_sequence"] + 1)


def test_finished_record_keeps_only_request_fingerprint() -> None:
    store = ExecutionStore(ImmediateEventRunner())
    started = store.create(
        {
            "task_id": "redact-request",
            "gateways": {"main_llm": {"api_key": "should-not-remain"}},
        }
    )
    execution_id = started["execution_id"]
    wait_until(lambda: snapshot_is_finished(store, execution_id))

    record = store._records[execution_id]
    assert record.request == {}
    assert len(record.request_fingerprint) == 64


def test_concurrent_create_allows_exactly_one_active_execution() -> None:
    runner = BlockingRunner()
    store = ExecutionStore(runner)
    barrier = threading.Barrier(8)

    def create(index: int) -> tuple[str, str]:
        barrier.wait(timeout=1)
        try:
            response = store.create({"task_id": f"task-{index}", "mode": "wait"})
            return "created", response["execution_id"]
        except ExecutionBusyError as exc:
            return "busy", exc.snapshot["execution_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(create, range(8)))

    created = [
        execution_id for outcome, execution_id in outcomes if outcome == "created"
    ]
    busy = [execution_id for outcome, execution_id in outcomes if outcome == "busy"]
    assert len(created) == 1
    assert busy == [created[0]] * 7

    store.cancel(created[0])
    runner.release.set()
    wait_until(lambda: snapshot_is_finished(store, created[0]))


def test_watermark_heavy_operation_keeps_compatible_lifecycle() -> None:
    store = ExecutionStore()
    abort_event = store.begin_heavy_operation("watermark-1")
    assert abort_event.is_set() is False
    assert store.current_snapshot()["status"] == "running"

    with pytest.raises(ExecutionBusyError):
        store.begin_heavy_operation("watermark-2")

    store.finish_heavy_operation("watermark-1")
    snapshot = store.snapshot("watermark-1")
    assert snapshot["status"] == "succeeded"
    assert snapshot["worker_finished"] is True
    assert [
        event.type
        for event in store.replay("watermark-1", snapshot["initial_sequence"])
    ][-1] == "result"


def test_abort_current_cancels_active_heavy_operation() -> None:
    store = ExecutionStore()
    abort_event = store.begin_heavy_operation("watermark-cancel")
    store.abort_current()
    assert abort_event.wait(timeout=1)
    assert store.snapshot("watermark-cancel")["status"] == "cancelling"

    store.finish_heavy_operation("watermark-cancel")
    snapshot = store.snapshot("watermark-cancel")
    assert snapshot["status"] == "cancelled"
    assert [
        event.type
        for event in store.replay("watermark-cancel", snapshot["initial_sequence"])
    ][-1] == "cancelled"


def test_failed_heavy_operation_records_error_terminal() -> None:
    store = ExecutionStore()
    store.begin_heavy_operation("watermark-failed")
    store.finish_heavy_operation(
        "watermark-failed",
        error_code="transform_failed",
        error_message="watermark transform failed",
    )

    snapshot = store.snapshot("watermark-failed")
    events = store.replay("watermark-failed", snapshot["initial_sequence"])
    assert snapshot["status"] == "failed"
    assert snapshot["worker_finished"] is True
    assert events[-1].type == "error"
    assert events[-1].payload["code"] == "transform_failed"
