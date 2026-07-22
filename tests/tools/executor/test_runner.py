from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import pytest
from babeldoc.tools.executor.protocol import WorkerEvent
from babeldoc.tools.executor.runner import MultiprocessExecutionRunner


def _result_then_cleanup_target(request, progress_send, _cancel_recv) -> None:
    progress_send.send({"type": "result", "payload": {"files": {}}})
    time.sleep(request["cleanup_delay"])
    Path(request["marker"]).write_text("clean", encoding="utf-8")
    progress_send.send(None)


def _duplicate_terminal_target(_request, progress_send, _cancel_recv) -> None:
    progress_send.send({"type": "result", "payload": {"files": {}}})
    progress_send.send(
        {
            "type": "error",
            "payload": {"code": "late_error", "message": "too late"},
        }
    )
    progress_send.send({"type": "cancelled", "payload": {"reason": "too_late"}})
    progress_send.send(None)


def _cooperative_cancel_target(_request, progress_send, cancel_recv) -> None:
    progress_send.send({"type": "progress", "payload": {"stage": "ready"}})
    cancel_recv.recv()
    progress_send.send({"type": "cancelled", "payload": {"reason": "cooperative"}})
    progress_send.send(
        {
            "type": "error",
            "payload": {"code": "late_error", "message": "too late"},
        }
    )
    progress_send.send(None)


def _ignore_cancel_target(_request, progress_send, _cancel_recv) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    progress_send.send({"type": "progress", "payload": {"stage": "ready"}})
    while True:
        time.sleep(1)


def _descendant_ignores_cancel_target(request, progress_send, _cancel_recv) -> None:
    descendant = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)",
        ]
    )
    Path(request["pid_file"]).write_text(str(descendant.pid), encoding="utf-8")
    progress_send.send({"type": "progress", "payload": {"stage": "ready"}})
    while True:
        time.sleep(1)


def _parent_loss_worker_target(request, _progress_send, cancel_recv) -> None:
    from babeldoc.tools.executor.babeldoc_adapter import _CancelState
    from babeldoc.tools.executor.babeldoc_adapter import _watch_cancel_pipe

    descendant = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "time.sleep(30)",
        ]
    )
    Path(request["pid_file"]).write_text(
        json.dumps(
            {
                "leader": os.getpid(),
                "descendant": descendant.pid,
            }
        ),
        encoding="utf-8",
    )
    _watch_cancel_pipe(cancel_recv, _CancelState(), threading.Event())


def _run_parent_loss_service(pid_file: str) -> None:
    MultiprocessExecutionRunner(
        _parent_loss_worker_target,
        start_method="spawn",
        poll_seconds=0.01,
        join_timeout_seconds=0.1,
    ).run(
        {"task_id": "parent-loss", "pid_file": pid_file},
        lambda _event: None,
        threading.Event(),
    )


def _runner(target, *, join_timeout: float = 0.5) -> MultiprocessExecutionRunner:
    return MultiprocessExecutionRunner(
        target,
        start_method="spawn",
        poll_seconds=0.005,
        join_timeout_seconds=join_timeout,
    )


def test_result_allows_child_normal_cleanup(tmp_path: Path) -> None:
    marker = tmp_path / "cleaned"
    events: list[WorkerEvent] = []

    _runner(_result_then_cleanup_target).run(
        {
            "task_id": "success",
            "marker": str(marker),
            "cleanup_delay": 0.05,
        },
        events.append,
        threading.Event(),
    )

    assert [event.type for event in events] == ["result"]
    assert marker.read_text(encoding="utf-8") == "clean"


def test_runner_emits_only_first_terminal_event() -> None:
    events: list[WorkerEvent] = []

    _runner(_duplicate_terminal_target).run(
        {"task_id": "duplicate-terminal"},
        events.append,
        threading.Event(),
    )

    assert [event.type for event in events] == ["result"]


def test_cooperative_cancel_emits_one_cancelled_terminal() -> None:
    abort_event = threading.Event()
    events: list[WorkerEvent] = []

    def emit(event: WorkerEvent) -> None:
        events.append(event)
        if event.type == "progress":
            abort_event.set()

    _runner(_cooperative_cancel_target).run(
        {"task_id": "cooperative-cancel"},
        emit,
        abort_event,
    )

    assert [event.type for event in events] == ["progress", "cancelled"]
    assert events[-1].payload == {"reason": "cooperative"}


def test_cancel_escalates_when_child_ignores_cooperative_and_sigterm() -> None:
    abort_event = threading.Event()
    events: list[WorkerEvent] = []

    def emit(event: WorkerEvent) -> None:
        events.append(event)
        if event.type == "progress":
            abort_event.set()

    started_at = time.monotonic()
    _runner(_ignore_cancel_target, join_timeout=0.05).run(
        {"task_id": "forced-cancel"},
        emit,
        abort_event,
    )

    assert time.monotonic() - started_at < 2
    assert [event.type for event in events] == ["progress", "cancelled"]
    assert events[-1].payload == {"reason": "client_request"}


def test_cancel_kills_descendants_in_the_worker_process_group(tmp_path: Path) -> None:
    abort_event = threading.Event()
    events: list[WorkerEvent] = []
    pid_file = tmp_path / "descendant-pid"

    def emit(event: WorkerEvent) -> None:
        events.append(event)
        if event.type == "progress":
            abort_event.set()

    _runner(_descendant_ignores_cancel_target, join_timeout=0.05).run(
        {"task_id": "descendant-cancel", "pid_file": str(pid_file)},
        emit,
        abort_event,
    )

    descendant_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not psutil.pid_exists(descendant_pid):
            break
        try:
            if psutil.Process(descendant_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("worker descendant survived cancellation")

    assert events[-1].type == "cancelled"


@pytest.mark.skipif(
    not hasattr(os, "setsid")
    or not hasattr(os, "getpgid")
    or not hasattr(os, "killpg")
    or not hasattr(signal, "SIGKILL"),
    reason="POSIX process groups are required",
)
def test_parent_loss_kills_worker_and_descendant_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "parent-loss-pids.json"
    command = (
        "from tests.tools.executor.test_runner import _run_parent_loss_service; "
        "import sys; _run_parent_loss_service(sys.argv[1])"
    )
    outer = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", command, str(pid_file)],
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    process_handles: list[psutil.Process] = []
    worker_pid: int | None = None
    try:
        deadline = time.monotonic() + 10
        process_ids: dict[str, int] | None = None
        while time.monotonic() < deadline:
            if outer.poll() is not None:
                raise AssertionError(
                    f"service-like parent exited before worker startup: {outer.returncode}"
                )
            try:
                loaded = json.loads(pid_file.read_text(encoding="utf-8"))
                process_ids = {
                    "leader": int(loaded["leader"]),
                    "descendant": int(loaded["descendant"]),
                }
                break
            except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
                time.sleep(0.01)
        if process_ids is None:
            raise AssertionError("worker process group did not become ready")

        worker_pid = process_ids["leader"]
        process_handles = [
            psutil.Process(worker_pid),
            psutil.Process(process_ids["descendant"]),
        ]
        assert os.getpgid(worker_pid) == worker_pid

        outer.send_signal(signal.SIGKILL)
        outer.wait(timeout=5)

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if all(_process_is_gone_or_zombie(process) for process in process_handles):
                break
            time.sleep(0.02)
        else:
            surviving = [
                process.pid
                for process in process_handles
                if not _process_is_gone_or_zombie(process)
            ]
            raise AssertionError(
                f"worker process group survived parent loss: {surviving}"
            )
    finally:
        cleanup_handles = list(process_handles)
        if outer.poll() is None:
            try:
                cleanup_handles.extend(
                    psutil.Process(outer.pid).children(recursive=True)
                )
            except psutil.NoSuchProcess:
                pass
            outer.kill()
        try:
            outer.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if worker_pid is not None:
            try:
                if os.getpgid(worker_pid) == worker_pid:
                    os.killpg(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in {process.pid: process for process in cleanup_handles}.values():
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    process.kill()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass


def _process_is_gone_or_zombie(process: psutil.Process) -> bool:
    try:
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True


def test_worker_event_coercion_rejects_invalid_payload() -> None:
    invalid: dict[str, Any] = {"type": "progress", "payload": "not-an-object"}

    try:
        MultiprocessExecutionRunner._coerce_event(invalid)
    except ValueError as error:
        assert str(error) == "subprocess emitted an invalid executor event"
    else:
        raise AssertionError("invalid worker event was accepted")
