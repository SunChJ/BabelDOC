from __future__ import annotations

import logging
import multiprocessing
import os
import shutil
import signal
import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psutil
from babeldoc.tools.executor.protocol import TERMINAL_EVENT_TYPES
from babeldoc.tools.executor.protocol import WorkerEvent

ProcessTarget = Callable[[dict[str, Any], Any, Any], None]
logger = logging.getLogger(__name__)
KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _configure_child_logging() -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    logging.basicConfig(level=logging.WARNING, force=True)

    executor_logger = logging.getLogger("babeldoc.tools.executor")
    executor_logger.handlers.clear()
    executor_logger.addHandler(handler)
    executor_logger.setLevel(logging.INFO)
    executor_logger.propagate = False


def _run_process_target(
    target: ProcessTarget,
    request: dict[str, Any],
    progress_send,
    cancel_recv,
) -> None:
    _isolate_process_group()
    _configure_child_logging()
    task_id = _task_id(request)
    started_at = time.monotonic()
    logger.info("executor subprocess target starting: task_id=%s", task_id)
    try:
        target(request, progress_send, cancel_recv)
    finally:
        elapsed = time.monotonic() - started_at
        logger.info(
            "executor subprocess target exited: task_id=%s elapsed=%.3fs",
            task_id,
            elapsed,
        )


def _forkserver_warmup_target() -> None:
    return


def _isolate_process_group() -> None:
    if not hasattr(os, "setsid"):
        return
    try:
        os.setsid()
    except OSError:
        logger.warning("executor subprocess could not create a private process group")


class ExecutionRunner:
    def run(
        self,
        request: dict[str, Any],
        emit: Callable[[WorkerEvent], None],
        abort_event: threading.Event,
    ) -> None:
        raise NotImplementedError


class UnavailableRunner(ExecutionRunner):
    def run(
        self,
        request: dict[str, Any],
        emit: Callable[[WorkerEvent], None],
        abort_event: threading.Event,
    ) -> None:
        emit(
            WorkerEvent(
                "error",
                {
                    "message": "executor runner is not configured",
                    "message_for_user": None,
                    "code": "runner_not_configured",
                },
            )
        )


class FakeExecutionRunner(ExecutionRunner):
    def run(
        self,
        request: dict[str, Any],
        emit: Callable[[WorkerEvent], None],
        abort_event: threading.Event,
    ) -> None:
        if request.get("mode") == "block":
            abort_event.wait(timeout=30)
            return
        if request.get("mode") == "burst":
            emit(WorkerEvent("progress", {"index": 1}))
            emit(WorkerEvent("progress", {"index": 2}))
            emit(
                WorkerEvent(
                    "result",
                    {
                        "files": {"dual_pdf": "dual.pdf"},
                        "metrics": {},
                        "usage": None,
                    },
                )
            )
            return

        input_path, output_dir = self._resolve_io(request)
        output_dir.mkdir(parents=True, exist_ok=True)
        emit(
            WorkerEvent(
                "progress",
                {
                    "type": "progress_update",
                    "stage": "fake_executor",
                    "overall_progress": 10,
                },
            )
        )
        if abort_event.wait(timeout=0.05):
            return

        files = {
            "dual": output_dir / "translated_dual.pdf",
            "mono": output_dir / "translated_mono.pdf",
            "no_watermark_dual": output_dir / "translated_dual_no_watermark.pdf",
            "no_watermark_mono": output_dir / "translated_mono_no_watermark.pdf",
        }
        for path in files.values():
            shutil.copyfile(input_path, path)

        auto_glossary = output_dir / "auto_extracted_glossary.csv"
        auto_glossary.write_text("source,target\n", encoding="utf-8")

        emit(
            WorkerEvent(
                "progress",
                {
                    "type": "progress_update",
                    "stage": "fake_executor",
                    "overall_progress": 90,
                },
            )
        )
        if abort_event.wait(timeout=0.05):
            return

        emit(
            WorkerEvent(
                "result",
                {
                    "files": {
                        "dual_pdf": str(files["dual"]),
                        "mono_pdf": str(files["mono"]),
                        "dual_no_watermark_pdf": str(files["no_watermark_dual"]),
                        "mono_no_watermark_pdf": str(files["no_watermark_mono"]),
                        "auto_extracted_glossary_csv": str(auto_glossary),
                    },
                    "metrics": {
                        "pdf_total_char_count": input_path.stat().st_size,
                        "pdf_total_char_token_count": 0,
                        "peak_memory_usage": 0,
                        "time_consume_seconds": 0.1,
                    },
                    "usage": {},
                },
            )
        )

    @staticmethod
    def _resolve_io(request: dict[str, Any]) -> tuple[Path, Path]:
        from babeldoc.tools.executor.workroot import get_workroot
        from babeldoc.tools.executor.workroot import resolve_dir
        from babeldoc.tools.executor.workroot import resolve_file

        paths = request.get("paths")
        if not isinstance(paths, dict):
            raise ValueError("paths must be an object")

        input_value = paths.get("input_file")
        output_value = paths.get("output_dir")
        if not isinstance(input_value, str) or not input_value:
            raise ValueError("paths.input_file is required")
        if not isinstance(output_value, str) or not output_value:
            raise ValueError("paths.output_dir is required")

        workroot = get_workroot(require_ready_file=True)
        input_path = resolve_file(workroot, input_value)
        output_dir = resolve_dir(workroot, output_value, create=True)
        return input_path, output_dir


class MultiprocessExecutionRunner(ExecutionRunner):
    def __init__(
        self,
        target: ProcessTarget,
        *,
        start_method: str = "spawn",
        preload_modules: Iterable[str] = (),
        poll_seconds: float = 0.05,
        join_timeout_seconds: float = 1.0,
    ):
        self._target = target
        self._start_method = start_method
        self._preload_modules = tuple(preload_modules)
        if start_method == "forkserver" and self._preload_modules:
            multiprocessing.set_forkserver_preload(list(self._preload_modules))
        self._context = multiprocessing.get_context(start_method)
        self._poll_seconds = poll_seconds
        self._join_timeout_seconds = join_timeout_seconds

    def warmup(self) -> None:
        if self._start_method != "forkserver":
            return

        started_at = time.monotonic()
        process = self._context.Process(target=_forkserver_warmup_target)
        process.start()
        process.join()
        elapsed = time.monotonic() - started_at
        if process.exitcode != 0:
            raise RuntimeError(
                f"executor forkserver warmup failed: exit_code={process.exitcode}"
            )
        logger.info(
            "executor forkserver warmup completed: elapsed=%.3fs preload_modules=%s",
            elapsed,
            ",".join(self._preload_modules) or "none",
        )

    def run(
        self,
        request: dict[str, Any],
        emit: Callable[[WorkerEvent], None],
        abort_event: threading.Event,
    ) -> None:
        task_id = _task_id(request)
        progress_recv, progress_send = self._context.Pipe(duplex=False)
        cancel_recv, cancel_send = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_run_process_target,
            args=(self._target, request, progress_send, cancel_recv),
        )
        started_at = time.monotonic()
        try:
            process.start()
        except BaseException:
            progress_recv.close()
            progress_send.close()
            cancel_recv.close()
            cancel_send.close()
            raise
        logger.info(
            "executor subprocess started: task_id=%s pid=%s start_elapsed=%.3fs",
            task_id,
            process.pid,
            time.monotonic() - started_at,
        )
        progress_send.close()
        cancel_recv.close()

        terminal_seen = False
        cancellation_requested = False
        cancellation_cleanup_completed = False
        try:
            while True:
                if abort_event.is_set():
                    cancellation_requested = True
                    logger.warning(
                        "executor subprocess cancellation requested: task_id=%s pid=%s",
                        task_id,
                        process.pid,
                    )
                    terminal_seen = self._cancel_process(
                        process,
                        progress_recv,
                        cancel_send,
                        emit,
                        terminal_seen,
                    )
                    cancellation_cleanup_completed = True
                    return

                if progress_recv.poll(self._poll_seconds):
                    try:
                        item = progress_recv.recv()
                    except EOFError:
                        process.join(timeout=0)
                        if not terminal_seen:
                            logger.error(
                                "executor subprocess pipe closed before terminal event: task_id=%s pid=%s exit_code=%s",
                                task_id,
                                process.pid,
                                process.exitcode,
                            )
                            self._emit_missing_terminal_error(process.exitcode, emit)
                            terminal_seen = True
                        return
                    if item is None:
                        if not terminal_seen:
                            process.join(timeout=0.2)
                            logger.error(
                                "executor subprocess ended before terminal event: task_id=%s pid=%s exit_code=%s",
                                task_id,
                                process.pid,
                                process.exitcode,
                            )
                            self._emit_missing_terminal_error(process.exitcode, emit)
                            terminal_seen = True
                        return

                    event = self._coerce_event(item)
                    terminal_seen = self._emit_unless_terminal_seen(
                        event,
                        emit,
                        terminal_seen,
                    )
                    if terminal_seen:
                        if event.type == "result":
                            logger.info(
                                "executor subprocess emitted terminal result: task_id=%s pid=%s",
                                task_id,
                                process.pid,
                            )
                        elif event.type == "error":
                            logger.warning(
                                "executor subprocess emitted terminal error: task_id=%s pid=%s code=%s message=%s",
                                task_id,
                                process.pid,
                                event.payload.get("code"),
                                event.payload.get("message"),
                            )
                        else:
                            logger.info(
                                "executor subprocess emitted terminal cancelled: task_id=%s pid=%s reason=%s",
                                task_id,
                                process.pid,
                                event.payload.get("reason"),
                            )
                        return
                    continue

                if not process.is_alive():
                    process.join(timeout=0)
                    terminal_seen = self._drain_progress(
                        progress_recv, emit, terminal_seen
                    )
                    if not terminal_seen:
                        logger.error(
                            "executor subprocess exited before terminal event: task_id=%s pid=%s exit_code=%s",
                            task_id,
                            process.pid,
                            process.exitcode,
                        )
                        self._emit_missing_terminal_error(process.exitcode, emit)
                        terminal_seen = True
                    return
        finally:
            try:
                if cancellation_requested and not cancellation_cleanup_completed:
                    self._stop_process(process)
                elif not cancellation_requested and terminal_seen:
                    self._wait_for_terminal_exit(process)
                elif not cancellation_requested:
                    self._stop_process(process)
            finally:
                cancel_send.close()
                progress_recv.close()

    @staticmethod
    def _coerce_event(item: Any) -> WorkerEvent:
        if isinstance(item, WorkerEvent):
            return item
        if isinstance(item, dict):
            event_type = item.get("type")
            payload = item.get("payload", item.get("data"))
            if isinstance(event_type, str) and isinstance(payload, dict):
                return WorkerEvent(event_type, payload)
        raise ValueError("subprocess emitted an invalid executor event")

    def _drain_progress(
        self,
        progress_recv: Any,
        emit: Callable[[WorkerEvent], None],
        terminal_seen: bool,
    ) -> bool:
        while not terminal_seen and progress_recv.poll():
            try:
                item = progress_recv.recv()
            except EOFError:
                return terminal_seen
            if item is None:
                return terminal_seen
            event = self._coerce_event(item)
            terminal_seen = self._emit_unless_terminal_seen(
                event,
                emit,
                terminal_seen,
            )
        return terminal_seen

    def _cancel_process(
        self,
        process: multiprocessing.Process,
        progress_recv: Any,
        cancel_send: Any,
        emit: Callable[[WorkerEvent], None],
        terminal_seen: bool,
    ) -> bool:
        process_group_id = process.pid
        self._send_cancel(cancel_send)
        deadline = time.monotonic() + self._join_timeout_seconds
        while process.is_alive() and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            if progress_recv.poll(min(self._poll_seconds, remaining)):
                try:
                    item = progress_recv.recv()
                except EOFError:
                    break
                if item is None:
                    break
                event = self._coerce_event(item)
                if event.type == "cancelled":
                    terminal_seen = self._emit_unless_terminal_seen(
                        event,
                        emit,
                        terminal_seen,
                    )
                elif event.type not in TERMINAL_EVENT_TYPES and not terminal_seen:
                    emit(event)
            process.join(timeout=0)

        if not process.is_alive():
            terminal_seen = self._drain_cancelled_event(
                progress_recv,
                emit,
                terminal_seen,
            )
        self._stop_process(process, process_group_id=process_group_id)
        if not terminal_seen:
            emit(WorkerEvent("cancelled", {"reason": "client_request"}))
            terminal_seen = True
        return terminal_seen

    def _drain_cancelled_event(
        self,
        progress_recv: Any,
        emit: Callable[[WorkerEvent], None],
        terminal_seen: bool,
    ) -> bool:
        while progress_recv.poll():
            try:
                item = progress_recv.recv()
            except EOFError:
                return terminal_seen
            if item is None:
                return terminal_seen
            event = self._coerce_event(item)
            if event.type == "cancelled":
                return self._emit_unless_terminal_seen(event, emit, terminal_seen)
            if event.type not in TERMINAL_EVENT_TYPES and not terminal_seen:
                emit(event)
        return terminal_seen

    @staticmethod
    def _emit_unless_terminal_seen(
        event: WorkerEvent,
        emit: Callable[[WorkerEvent], None],
        terminal_seen: bool,
    ) -> bool:
        if terminal_seen:
            logger.warning(
                "executor ignored event after terminal event: type=%s",
                event.type,
            )
            return True
        emit(event)
        return event.type in TERMINAL_EVENT_TYPES

    @staticmethod
    def _send_cancel(cancel_send: Any) -> None:
        try:
            cancel_send.send(True)
        except (BrokenPipeError, EOFError, OSError):
            return

    @staticmethod
    def _emit_missing_terminal_error(
        exitcode: int | None,
        emit: Callable[[WorkerEvent], None],
    ) -> None:
        exit_suffix = "unknown" if exitcode is None else str(exitcode)
        emit(
            WorkerEvent(
                "error",
                {
                    "message": (
                        "executor subprocess ended without a terminal "
                        f"event: exit_code={exit_suffix}"
                    ),
                    "message_for_user": None,
                    "code": "missing_terminal_event",
                },
            )
        )

    def _wait_for_terminal_exit(self, process: multiprocessing.Process) -> None:
        process_group_id = process.pid
        process.join(timeout=self._join_timeout_seconds)
        if process.is_alive() or self._process_group_exists(process_group_id):
            logger.warning(
                "executor subprocess group did not exit after terminal event: pid=%s",
                process.pid,
            )
            self._stop_process(process, process_group_id=process_group_id)

    def _stop_process(
        self,
        process: multiprocessing.Process,
        *,
        process_group_id: int | None = None,
    ) -> None:
        process_group_id = process_group_id or process.pid
        group_exists = self._process_group_exists(process_group_id)
        if not process.is_alive() and not group_exists:
            process.join(timeout=0)
            return
        logger.warning(
            "executor subprocess group still alive during cleanup; terminating pid=%s",
            process.pid,
        )
        self._signal_process(
            process,
            signal.SIGTERM,
            process_group_id=process_group_id,
        )
        deadline = time.monotonic() + self._join_timeout_seconds
        while time.monotonic() < deadline:
            process.join(timeout=0)
            if not process.is_alive() and not self._process_group_exists(
                process_group_id
            ):
                return
            time.sleep(min(self._poll_seconds, max(0.0, deadline - time.monotonic())))
        if process.is_alive() or self._process_group_exists(process_group_id):
            logger.error(
                "executor subprocess group did not terminate; killing pid=%s",
                process.pid,
            )
            self._signal_process(
                process,
                KILL_SIGNAL,
                process_group_id=process_group_id,
                force=True,
            )
            process.join(timeout=self._join_timeout_seconds)
            if process.is_alive() or self._process_group_exists(process_group_id):
                logger.critical(
                    "executor subprocess group survived SIGKILL deadline: pid=%s",
                    process.pid,
                )

    @staticmethod
    def _signal_process(
        process: multiprocessing.Process,
        signal_number: int,
        *,
        process_group_id: int | None = None,
        force: bool = False,
    ) -> None:
        process_group_id = process_group_id or process.pid
        if process_group_id is None:
            return
        if hasattr(os, "killpg"):
            try:
                os.killpg(process_group_id, signal_number)
                return
            except ProcessLookupError:
                if not process.is_alive():
                    return
            except OSError:
                pass
        try:
            if force:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            return

    @staticmethod
    def _process_group_exists(process_group_id: int | None) -> bool:
        if process_group_id is None or not hasattr(os, "killpg"):
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        for member in psutil.process_iter(["pid", "status"]):
            try:
                if (
                    os.getpgid(member.pid) == process_group_id
                    and member.info["status"] != psutil.STATUS_ZOMBIE
                ):
                    return True
            except (OSError, psutil.Error):
                continue
        return False


def _task_id(request: dict[str, Any]) -> str:
    value = request.get("task_id")
    return value if isinstance(value, str) and value else "unknown"
