from __future__ import annotations

import argparse
import hmac
import json
import logging
import multiprocessing
import os
import secrets
import signal
import stat
import string
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

import psutil
from babeldoc.tools.executor.protocol import MAX_SEQUENCE
from babeldoc.tools.executor.runner import FakeExecutionRunner
from babeldoc.tools.executor.runner import MultiprocessExecutionRunner
from babeldoc.tools.executor.state import CursorAheadError
from babeldoc.tools.executor.state import ExecutionBusyError
from babeldoc.tools.executor.state import ExecutionConflictError
from babeldoc.tools.executor.state import ExecutionNotFoundError
from babeldoc.tools.executor.state import ExecutionStore
from babeldoc.tools.executor.state import ReplayGapError
from babeldoc.tools.executor.workroot import WORKROOT_ENV
from babeldoc.tools.executor.workroot import WORKROOT_READY_FILE
from babeldoc.tools.executor.workroot import get_workroot
from babeldoc.tools.executor.workroot import relative_to_workroot
from babeldoc.tools.executor.workroot import resolve_file
from babeldoc.tools.executor.workroot import resolve_inside_workroot

logger = logging.getLogger(__name__)
WATERMARK_TIMEOUT_SECONDS = 600
WATERMARK_POLL_SECONDS = 0.05
WATERMARK_TERMINATE_TIMEOUT_SECONDS = 1.0
WATERMARK_KILL_TIMEOUT_SECONDS = 5.0
MAX_JSON_BODY_BYTES = 1024 * 1024
READY_PREFIX = "__GLOSS_BABELDOC_SERVICE_READY__"
SERVICE_ID = "gloss-babeldoc"
SCHEMA_VERSION = 1
REQUEST_TIMEOUT_SECONDS = 10.0
SHUTDOWN_CANCEL_TIMEOUT_SECONDS = 5.0
SHUTDOWN_DRAIN_POLL_SECONDS = 0.25
PARENT_WATCHDOG_INTERVAL_SECONDS = 1.0
EVENT_HEARTBEAT_INTERVAL_SECONDS = 5.0
_TOKEN_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-_")
ALLOW_FAKE_RUNNER_ENV = "BABELDOC_EXECUTOR_ALLOW_FAKE"


def _dispatch_watermark_operation(
    operation: str,
    input_file: Path,
    output_file: Path,
    asset_files: tuple[Path, ...],
) -> None:
    from babeldoc.tools.executor.watermark_transform import add_corner_watermark
    from babeldoc.tools.executor.watermark_transform import add_tiled_watermark

    if operation == "watermark1":
        if len(asset_files) != 1:
            raise ValueError("watermark1 requires exactly one asset")
        add_tiled_watermark(input_file, output_file, asset_files[0])
        return
    if operation == "watermark2":
        if len(asset_files) != 2:
            raise ValueError("watermark2 requires exactly two assets")
        add_corner_watermark(
            input_file,
            output_file,
            asset_files[0],
            asset_files[1],
        )
        return
    raise ValueError("unsupported watermark operation")


def _run_watermark_process_target(
    operation: str,
    input_file: Path,
    output_file: Path,
    asset_files: tuple[Path, ...],
) -> None:
    if hasattr(os, "setsid"):
        try:
            os.setsid()
        except OSError:
            pass
    try:
        _dispatch_watermark_operation(
            operation,
            input_file,
            output_file,
            asset_files,
        )
    except BaseException:
        # Keep request-derived paths out of child tracebacks while preserving a
        # non-zero exit status for the supervising service.
        raise SystemExit(1) from None


def _watermark_process_group_exists(process_group_id: int | None) -> bool:
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


def _signal_watermark_process(
    process: multiprocessing.Process,
    signal_number: int,
    *,
    force: bool = False,
) -> None:
    process_group_id = process.pid
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


def _stop_watermark_process(process: multiprocessing.Process) -> None:
    process_group_id = process.pid
    try:
        if not process.is_alive() and not _watermark_process_group_exists(
            process_group_id
        ):
            process.join(timeout=0)
            return

        _signal_watermark_process(process, signal.SIGTERM)
        deadline = time.monotonic() + WATERMARK_TERMINATE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            process.join(timeout=0)
            if not process.is_alive() and not _watermark_process_group_exists(
                process_group_id
            ):
                return
            time.sleep(
                min(
                    WATERMARK_POLL_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                )
            )

        if process.is_alive() or _watermark_process_group_exists(process_group_id):
            _signal_watermark_process(
                process,
                getattr(signal, "SIGKILL", signal.SIGTERM),
                force=True,
            )
            process.join(timeout=WATERMARK_KILL_TIMEOUT_SECONDS)
            if process.is_alive() or _watermark_process_group_exists(process_group_id):
                logger.critical(
                    "watermark subprocess group survived cleanup: pid=%s",
                    process.pid,
                )
    finally:
        if not process.is_alive():
            process.close()


class RequestBodyError(ValueError):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


class ServiceStoppingError(RuntimeError):
    pass


class ExecutorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address,
        store: ExecutionStore,
        *,
        token: str,
        instance_id: str | None = None,
        parent_pid: int | None = None,
        parent_start_time: float | None = None,
        runner_name: str = "injected",
        workroot: Path | None = None,
        event_heartbeat_interval_seconds: float = (EVENT_HEARTBEAT_INTERVAL_SECONDS),
    ):
        if not (
            0 < event_heartbeat_interval_seconds <= EVENT_HEARTBEAT_INTERVAL_SECONDS
        ):
            raise ValueError(
                "event_heartbeat_interval_seconds must be greater than zero "
                "and no more than five seconds"
            )
        super().__init__(server_address, ExecutorHandler)
        self.store = store
        self.token = token
        self.instance_id = instance_id or str(uuid.uuid4())
        self.parent_pid = parent_pid
        self.parent_start_time = parent_start_time
        self.runner_name = runner_name
        self.workroot = workroot
        self.event_heartbeat_interval_seconds = event_heartbeat_interval_seconds
        self.started_at = time.time()
        self._stopping = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._shutdown_worker_started = False
        self._shutdown_cancel_active = False
        self._watchdog_stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def identity_payload(self) -> dict:
        host, port = self.server_address[:2]
        return {
            "schema_version": SCHEMA_VERSION,
            "protocol_version": 1,
            "service_id": SERVICE_ID,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "process_start_time": _process_start_time(os.getpid()),
            "endpoint": f"http://{host}:{port}",
            "started_at": self.started_at,
            "runner": self.runner_name,
            "parent_pid": self.parent_pid,
            "parent_start_time": self.parent_start_time,
        }

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(REQUEST_TIMEOUT_SECONDS)
        return request, client_address

    def admit_execution(self, request: dict) -> dict:
        with self._lifecycle_lock:
            if self._stopping.is_set():
                raise ServiceStoppingError
            return self.store.create(request)

    def begin_heavy_operation(self, operation_id: str) -> threading.Event:
        with self._lifecycle_lock:
            if self._stopping.is_set():
                raise ServiceStoppingError
            return self.store.begin_heavy_operation(operation_id)

    def start_parent_watchdog(self) -> None:
        if self.parent_pid is None or self._watchdog is not None:
            return
        self._watchdog = threading.Thread(
            target=self._watch_parent,
            name="gloss-babeldoc-parent-watchdog",
            daemon=True,
        )
        self._watchdog.start()

    def request_shutdown(
        self,
        *,
        cancel_active: bool = True,
        cancel_reason: str = "service_shutdown",
        defer_server_stop: bool = False,
    ) -> bool:
        with self._lifecycle_lock:
            self._shutdown_cancel_active = self._shutdown_cancel_active or cancel_active
            if cancel_active:
                self._watchdog_stop.set()
            if self._stopping.is_set():
                if cancel_active:
                    self.store.abort_current(reason=cancel_reason)
                started = False
            else:
                self._stopping.set()
                if cancel_active:
                    self.store.abort_current(reason=cancel_reason)
                started = True
        if not defer_server_stop:
            self._ensure_shutdown_worker()
        return started

    def _ensure_shutdown_worker(self) -> None:
        with self._lifecycle_lock:
            if self._shutdown_worker_started:
                return
            self._shutdown_worker_started = True
        threading.Thread(
            target=self._finish_shutdown,
            name="gloss-babeldoc-shutdown",
            daemon=True,
        ).start()

    def _finish_shutdown(self) -> None:
        wait_until_idle = getattr(self.store, "wait_until_idle", None)
        if callable(wait_until_idle):
            while True:
                with self._lifecycle_lock:
                    cancel_active = self._shutdown_cancel_active
                timeout_seconds = (
                    SHUTDOWN_CANCEL_TIMEOUT_SECONDS
                    if cancel_active
                    else SHUTDOWN_DRAIN_POLL_SECONDS
                )
                if wait_until_idle(timeout_seconds=timeout_seconds):
                    break
                if cancel_active:
                    logger.warning(
                        "executor shutdown timed out waiting for active worker"
                    )
                    break
        try:
            self.shutdown()
        finally:
            self._watchdog_stop.set()

    def _watch_parent(self) -> None:
        while not self._watchdog_stop.wait(PARENT_WATCHDOG_INTERVAL_SECONDS):
            if _process_matches(self.parent_pid, self.parent_start_time):
                continue
            logger.warning("executor parent disappeared; shutting down")
            self.request_shutdown(
                cancel_active=True,
                cancel_reason="parent_exit",
            )
            return


class ExecutorHandler(BaseHTTPRequestHandler):
    server: ExecutorServer

    def do_GET(self):
        if not self._authenticate():
            return

        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if parsed.path == "/healthz":
            payload = self.server.identity_payload()
            workroot_error = _workroot_health_error(self.server.workroot)
            payload.update(
                {
                    "status": (
                        "stopping"
                        if self.server.stopping
                        else "error"
                        if workroot_error
                        else "ok"
                    ),
                    "ok": not self.server.stopping and workroot_error is None,
                }
            )
            if workroot_error is not None:
                payload["code"] = "workroot_unavailable"
            self._write_json(
                HTTPStatus.SERVICE_UNAVAILABLE
                if self.server.stopping or workroot_error is not None
                else HTTPStatus.OK,
                payload,
            )
            return

        if parsed.path == "/v1/runtime":
            from babeldoc.gloss_cli import build_runtime_info

            payload = build_runtime_info()
            payload["service"] = self.server.identity_payload()
            self._write_json(HTTPStatus.OK, payload)
            return

        if parsed.path == "/v1/executions/current":
            self._write_json(
                HTTPStatus.OK,
                {"execution": _active_snapshot(self.server.store)},
            )
            return

        if parsed.path == "/v1/executions/latest":
            self._write_json(
                HTTPStatus.OK,
                {"execution": _latest_snapshot(self.server.store)},
            )
            return

        if (
            len(parts) == 4
            and parts[:2] == ["v1", "executions"]
            and parts[3] == "events"
        ):
            query = parse_qs(parsed.query)
            try:
                after_seq = int(query["after_sequence"][0])
            except ValueError:
                self._write_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "after_sequence must be an integer",
                )
                return
            except (KeyError, IndexError):
                self._write_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "after_sequence is required",
                )
                return
            if not 0 <= after_seq <= MAX_SEQUENCE:
                self._write_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "after_sequence is outside the supported range",
                )
                return
            self._stream_events(parts[2], after_seq)
            return

        if len(parts) == 3 and parts[:2] == ["v1", "executions"]:
            self._execution_snapshot(parts[2])
            return

        self._write_error(HTTPStatus.NOT_FOUND, "not_found", "not found")

    def do_POST(self):
        if not self._authenticate():
            return

        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if parsed.path == "/v1/executions":
            self._create_execution()
            return

        if (
            len(parts) == 4
            and parts[:2] == ["v1", "executions"]
            and parts[3] == "cancel"
        ):
            self._cancel_execution(parts[2])
            return

        if parsed.path == "/v1/shutdown":
            self._shutdown()
            return

        if parsed.path in {"/v1/pdf/watermark1", "/v1/pdf/watermark2"}:
            self._run_watermark(parsed.path.rsplit("/", 1)[-1])
            return

        self._write_error(HTTPStatus.NOT_FOUND, "not_found", "not found")

    def log_message(self, fmt, *args):
        if self.path == "/healthz":
            logger.debug(fmt, *args)
            return
        logger.debug(fmt, *args)

    def _authenticate(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        scheme, separator, presented = authorization.partition(" ")
        try:
            token_matches = hmac.compare_digest(
                presented.encode("ascii"),
                self.server.token.encode("ascii"),
            )
        except UnicodeEncodeError:
            token_matches = False
        if separator and scheme.lower() == "bearer" and token_matches:
            return True
        self._write_error(
            HTTPStatus.UNAUTHORIZED,
            "unauthorized",
            "a valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
        return False

    def _execution_snapshot(self, execution_id: str) -> None:
        try:
            snapshot = self.server.store.snapshot(execution_id)
        except ExecutionNotFoundError:
            self._write_error(
                HTTPStatus.NOT_FOUND,
                "execution_not_found",
                "execution not found",
            )
            return
        self._write_json(HTTPStatus.OK, snapshot)

    def _create_execution(self):
        request: dict | None = None
        try:
            request = self._read_json_body()
            response = self.server.admit_execution(request)
        except RequestBodyError as exc:
            self._write_error(exc.status, "invalid_request", str(exc))
            return
        except ValueError as exc:
            self._write_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return
        except ServiceStoppingError:
            self._write_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "service_stopping",
                "executor service is stopping",
            )
            return
        except ExecutionConflictError as exc:
            self._write_json(
                HTTPStatus.CONFLICT,
                {
                    "code": "idempotency_conflict",
                    "message": "task_id was already used for a different request",
                    "snapshot": exc.snapshot,
                },
            )
            return
        except ExecutionBusyError as exc:
            logger.warning(
                "executor rejected create because it is busy: requested_task_id=%s active_task_id=%s active_execution_id=%s",
                request.get("task_id") if isinstance(request, dict) else None,
                exc.snapshot.get("task_id"),
                exc.snapshot.get("execution_id"),
            )
            self._write_json(
                HTTPStatus.CONFLICT,
                {
                    "code": "busy",
                    "message": "executor is busy",
                    "snapshot": exc.snapshot,
                },
            )
            return

        logger.info(
            "executor accepted task: task_id=%s execution_id=%s initial_sequence=%s",
            request.get("task_id"),
            response.get("execution_id"),
            response.get("initial_sequence"),
        )
        self._write_json(
            HTTPStatus.OK if response.get("replayed") else HTTPStatus.CREATED,
            response,
        )

    def _cancel_execution(self, execution_id: str) -> None:
        try:
            self._read_json_body()
            response = _cancel_execution(self.server.store, execution_id)
        except RequestBodyError as exc:
            self._write_error(exc.status, "invalid_request", str(exc))
            return
        except ExecutionNotFoundError:
            self._write_error(
                HTTPStatus.NOT_FOUND,
                "execution_not_found",
                "execution not found",
            )
            return
        self._write_json(HTTPStatus.ACCEPTED, response)

    def _shutdown(self) -> None:
        try:
            request = self._read_json_body()
        except RequestBodyError as exc:
            self._write_error(exc.status, "invalid_request", str(exc))
            return
        cancel_active = request.get("cancel_active", True)
        if not isinstance(cancel_active, bool):
            self._write_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "cancel_active must be a boolean",
            )
            return
        started = self.server.request_shutdown(
            cancel_active=cancel_active,
            defer_server_stop=True,
        )
        try:
            self._write_json(
                HTTPStatus.ACCEPTED,
                {"status": "stopping", "already_stopping": not started},
            )
        finally:
            self.server._ensure_shutdown_worker()

    def _run_watermark(self, operation: str) -> None:
        try:
            request = self._read_json_body()
            workroot = self.server.workroot
            if workroot is None:
                raise ValueError("executor workroot is unavailable")
            operation_id, input_file, output_file, asset_files = (
                self._validate_watermark_request(workroot, operation, request)
            )
            abort_event = self.server.begin_heavy_operation(operation_id)
        except RequestBodyError as exc:
            self._write_error(exc.status, "invalid_request", str(exc))
            return
        except FileNotFoundError as exc:
            self._write_error(
                HTTPStatus.BAD_REQUEST,
                "input_missing" if "input_file" in str(exc) else "asset_missing",
                "input or asset file is missing",
            )
            return
        except ValueError as exc:
            self._write_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return
        except ServiceStoppingError:
            self._write_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "service_stopping",
                "executor service is stopping",
            )
            return
        except ExecutionConflictError as exc:
            self._write_json(
                HTTPStatus.CONFLICT,
                {
                    "code": "idempotency_conflict",
                    "message": "operation_id was already used",
                    "snapshot": exc.snapshot,
                },
            )
            return
        except ExecutionBusyError as exc:
            self._write_json(
                HTTPStatus.CONFLICT,
                {
                    "code": "busy",
                    "message": "executor is busy",
                    "snapshot": exc.snapshot,
                },
            )
            return

        error_code: str | None = None
        error_message: str | None = None
        error_status = HTTPStatus.INTERNAL_SERVER_ERROR
        output_relative: str | None = None
        try:
            input_relative = relative_to_workroot(workroot, input_file)
            output_relative = relative_to_workroot(workroot, output_file)
            logger.info(
                "executor watermark operation started: operation=%s operation_id=%s input=%s output=%s assets=%s",
                operation,
                operation_id,
                input_relative,
                output_relative,
                len(asset_files),
            )
            self._run_watermark_process(
                operation,
                input_file,
                output_file,
                asset_files,
                abort_event,
            )
        except TimeoutError:
            if abort_event.is_set():
                error_code = "operation_cancelled"
                error_message = "watermark transform was cancelled"
                error_status = HTTPStatus.CONFLICT
                logger.warning(
                    "executor watermark operation cancelled: operation=%s operation_id=%s",
                    operation,
                    operation_id,
                )
            else:
                error_code = "transform_timeout"
                error_message = "watermark transform timed out"
                logger.exception(
                    "executor watermark operation timed out: operation=%s operation_id=%s",
                    operation,
                    operation_id,
                )
        except Exception:
            error_code = "transform_failed"
            error_message = "watermark transform failed"
            logger.exception(
                "executor watermark operation failed: operation=%s operation_id=%s",
                operation,
                operation_id,
            )
        finally:
            self.server.store.finish_heavy_operation(
                operation_id,
                error_code=error_code,
                error_message=error_message,
            )
        if error_code is not None:
            self._write_error(error_status, error_code, error_message or error_code)
            return
        if output_relative is None:  # pragma: no cover - guarded by success path
            raise AssertionError("watermark output path was not resolved")
        self._write_json(
            HTTPStatus.OK,
            {
                "operation_id": operation_id,
                "output_file": output_relative,
            },
        )
        logger.info(
            "executor watermark operation finished: operation=%s operation_id=%s output=%s",
            operation,
            operation_id,
            output_relative,
        )

    @staticmethod
    def _validate_watermark_request(
        workroot: Path,
        operation: str,
        request: dict,
    ) -> tuple[str, Path, Path, list[Path]]:
        operation_id = request.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id is required")
        options = request.get("options")
        if options is not None and options != {}:
            raise ValueError("options must be an empty object")

        input_value = request.get("input_file")
        output_value = request.get("output_file")
        if not isinstance(input_value, str) or not input_value:
            raise ValueError("input_file is required")
        if not isinstance(output_value, str) or not output_value:
            raise ValueError("output_file is required")

        try:
            input_file = resolve_file(workroot, input_value)
        except FileNotFoundError as exc:
            raise FileNotFoundError("input_file") from exc
        asset_files = ExecutorHandler._resolve_watermark_assets(
            workroot,
            operation,
            request,
        )
        output_file = resolve_inside_workroot(workroot, output_value)
        if input_file == output_file:
            raise ValueError("output_file must not overwrite input_file")
        if output_file in asset_files:
            raise ValueError("output_file must not overwrite an asset file")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        return operation_id, input_file, output_file, asset_files

    @staticmethod
    def _resolve_watermark_assets(
        workroot: Path,
        operation: str,
        request: dict,
    ) -> list[Path]:
        if operation == "watermark1":
            asset_value = request.get("asset_file")
            if not isinstance(asset_value, str) or not asset_value:
                raise ValueError("asset_file is required")
            try:
                return [resolve_file(workroot, asset_value)]
            except FileNotFoundError as exc:
                raise FileNotFoundError("asset_file") from exc

        asset_value_1 = request.get("asset_file_1")
        asset_value_2 = request.get("asset_file_2")
        if not isinstance(asset_value_1, str) or not asset_value_1:
            raise ValueError("asset_file_1 is required")
        if not isinstance(asset_value_2, str) or not asset_value_2:
            raise ValueError("asset_file_2 is required")
        try:
            asset_file_1 = resolve_file(workroot, asset_value_1)
            asset_file_2 = resolve_file(workroot, asset_value_2)
        except FileNotFoundError as exc:
            raise FileNotFoundError("asset_file") from exc
        return [asset_file_1, asset_file_2]

    @staticmethod
    def _run_watermark_process(
        operation: str,
        input_file: Path,
        output_file: Path,
        asset_files: list[Path],
        abort_event,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_run_watermark_process_target,
            args=(
                operation,
                input_file,
                output_file,
                tuple(asset_files),
            ),
        )
        try:
            process.start()
        except BaseException:
            process.close()
            raise
        deadline = time.monotonic() + WATERMARK_TIMEOUT_SECONDS
        try:
            while True:
                if not process.is_alive():
                    process.join(timeout=0)
                    if process.exitcode != 0:
                        raise RuntimeError("watermark transform failed")
                    if not output_file.is_file():
                        raise RuntimeError("watermark transform did not create output")
                    return
                if abort_event.is_set():
                    raise TimeoutError("watermark transform aborted")
                if time.monotonic() >= deadline:
                    raise TimeoutError("watermark transform timed out")
                time.sleep(WATERMARK_POLL_SECONDS)
        finally:
            _stop_watermark_process(process)

    def _stream_events(self, execution_id: str, after_seq: int):
        try:
            self.server.store.replay(execution_id, after_seq)
        except CursorAheadError:
            self._write_json(
                HTTPStatus.CONFLICT,
                {
                    "code": "cursor_ahead",
                    "message": "requested sequence is ahead of the execution",
                    "snapshot": self._snapshot_or_none(execution_id),
                },
            )
            return
        except ReplayGapError:
            logger.error(
                "executor event stream replay gap: execution_id=%s after_sequence=%s",
                execution_id,
                after_seq,
            )
            self._write_json(
                HTTPStatus.GONE,
                {
                    "code": "replay_gap",
                    "message": "requested sequence is no longer available",
                    "snapshot": self._snapshot_or_none(execution_id),
                },
            )
            return
        except ExecutionNotFoundError:
            logger.warning(
                "executor event stream requested unknown execution: execution_id=%s after_sequence=%s",
                execution_id,
                after_seq,
            )
            self._write_error(
                HTTPStatus.NOT_FOUND,
                "execution_not_found",
                "execution not found",
            )
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        logger.info(
            "executor event stream attached: execution_id=%s after_sequence=%s",
            execution_id,
            after_seq,
        )
        cursor = after_seq
        try:
            for event in self.server.store.stream(
                execution_id,
                after_seq,
                heartbeat_interval_seconds=(
                    self.server.event_heartbeat_interval_seconds
                ),
            ):
                if event is None:
                    self.wfile.write(
                        _heartbeat_json_line(
                            execution_id=execution_id,
                            instance_id=self.server.instance_id,
                        )
                    )
                    self.wfile.flush()
                    continue
                cursor = event.sequence
                self.wfile.write(
                    _event_json_line(event, instance_id=self.server.instance_id)
                )
                self.wfile.flush()
        except (ReplayGapError, ExecutionNotFoundError) as exc:
            code = (
                "replay_gap"
                if isinstance(exc, ReplayGapError)
                else "execution_not_found"
            )
            message = (
                "event history changed while the stream was attached"
                if isinstance(exc, ReplayGapError)
                else "execution is no longer retained"
            )
            try:
                self.wfile.write(
                    _stream_error_json_line(
                        execution_id=execution_id,
                        after_sequence=cursor,
                        instance_id=self.server.instance_id,
                        code=code,
                        message=message,
                        snapshot=self._snapshot_or_none(execution_id),
                    )
                )
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                pass
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            logger.warning(
                "executor event stream disconnected: execution_id=%s after_sequence=%s",
                execution_id,
                after_seq,
            )
        logger.info(
            "executor event stream ended: execution_id=%s after_sequence=%s",
            execution_id,
            after_seq,
        )

    def _snapshot_or_none(self, execution_id: str) -> dict | None:
        try:
            return self.server.store.snapshot(execution_id)
        except ExecutionNotFoundError:
            return None

    def _read_json_body(self) -> dict:
        if self.headers.get("Transfer-Encoding") is not None:
            raise RequestBodyError("Transfer-Encoding is not supported")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RequestBodyError("Content-Length must be an integer") from exc
        if length < 0:
            raise RequestBodyError("Content-Length must not be negative")
        if length > MAX_JSON_BODY_BYTES:
            raise RequestBodyError(
                "json body exceeds 1 MiB limit",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, OSError) as exc:
            raise RequestBodyError(
                "request body timed out",
                HTTPStatus.REQUEST_TIMEOUT,
            ) from exc
        if len(raw) != length:
            raise RequestBodyError("request body ended before Content-Length")
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise RequestBodyError("invalid json") from exc
        if not isinstance(payload, dict):
            raise RequestBodyError("json body must be an object")
        return payload

    def _write_json(
        self,
        status: HTTPStatus,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
    ):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _write_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ):
        self._write_json(
            status,
            {"code": code, "message": message},
            headers=headers,
        )


def _is_loopback_host(host: str) -> bool:
    """Keep v1 on an unambiguous IPv4 loopback endpoint."""
    return host == "127.0.0.1"


def _event_json_line(event, *, instance_id: str) -> bytes:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "service_id": SERVICE_ID,
        "instance_id": instance_id,
        "type": event.type,
        "execution_id": event.execution_id,
        "sequence": event.sequence,
        "emitted_at": event.emitted_at,
        "payload": event.payload,
    }
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _heartbeat_json_line(*, execution_id: str, instance_id: str) -> bytes:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "service_id": SERVICE_ID,
        "instance_id": instance_id,
        "type": "heartbeat",
        "execution_id": execution_id,
        "sequence": None,
        "emitted_at": time.time(),
        "payload": {},
    }
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _stream_error_json_line(
    *,
    execution_id: str,
    after_sequence: int,
    instance_id: str,
    code: str,
    message: str,
    snapshot: dict | None,
) -> bytes:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "service_id": SERVICE_ID,
        "instance_id": instance_id,
        "type": "stream_error",
        "execution_id": execution_id,
        "sequence": None,
        "emitted_at": time.time(),
        "payload": {
            "code": code,
            "message": message,
            "after_sequence": after_sequence,
            "snapshot": snapshot,
        },
    }
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _active_snapshot(store: ExecutionStore) -> dict | None:
    active_snapshot = getattr(store, "active_snapshot", None)
    if callable(active_snapshot):
        return active_snapshot()
    return _latest_snapshot(store)


def _latest_snapshot(store: ExecutionStore) -> dict | None:
    current_snapshot = getattr(store, "current_snapshot", None)
    if callable(current_snapshot):
        return current_snapshot()
    record = getattr(store, "_current", None)
    if record is None:
        return None
    try:
        return store.snapshot(record.execution_id)
    except ExecutionNotFoundError:
        return None


def _cancel_execution(store: ExecutionStore, execution_id: str) -> dict:
    cancel = getattr(store, "cancel", None)
    if callable(cancel):
        return cancel(execution_id)
    snapshot = store.snapshot(execution_id)
    current = _active_snapshot(store)
    if current is None or current.get("execution_id") != execution_id:
        raise ExecutionNotFoundError(execution_id)
    store.abort_current()
    return {**snapshot, "status": "aborted"}


def _process_matches(process_id: int | None, start_time: float | None) -> bool:
    if process_id is None or process_id <= 0:
        return False
    try:
        process = psutil.Process(process_id)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        if start_time is None:
            return True
        return abs(process.create_time() - start_time) <= 0.01
    except (psutil.Error, OSError, ValueError):
        return False


def _process_start_time(process_id: int) -> float | None:
    try:
        process = psutil.Process(process_id)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process.create_time()
    except (psutil.Error, OSError, ValueError):
        return None


def _resolve_parent_identity(
    process_id: int | None,
    supplied_start_time: float | None,
) -> tuple[int | None, float | None]:
    if process_id is None:
        if supplied_start_time is not None:
            raise ValueError("parent_start_time requires parent_pid")
        return None, None
    if process_id <= 0:
        raise ValueError("parent_pid must be positive")
    actual_start_time = _process_start_time(process_id)
    if actual_start_time is None:
        raise ValueError("parent process is not running")
    if (
        supplied_start_time is not None
        and abs(actual_start_time - supplied_start_time) > 0.01
    ):
        raise ValueError("parent process identity does not match parent_start_time")
    return process_id, actual_start_time


def _load_token(token_file: str | Path | None) -> tuple[str, bool]:
    if token_file is not None:
        path = Path(token_file)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ValueError("unable to read executor token file") from exc
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError("executor token file must be a regular file")
            if hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
                raise ValueError("executor token file must be owned by this user")
            if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
                raise ValueError("executor token file permissions must be 0600")
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        if len(raw) > 4096:
            raise ValueError("executor token file is too large")
        try:
            value = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise ValueError("executor token must be ASCII") from exc
        if not 32 <= len(value) <= 256 or any(
            character not in _TOKEN_CHARACTERS for character in value
        ):
            raise ValueError("executor token must be 32-256 URL-safe characters")
        return value, False
    return secrets.token_urlsafe(32), True


def _configure_workroot(
    work_dir: str | Path | None,
    *,
    required: bool,
) -> Path | None:
    if work_dir is None and not required:
        return None
    if work_dir is not None:
        path = Path(work_dir).resolve()
        os.environ[WORKROOT_ENV] = str(path)
    workroot = get_workroot(require_ready_file=True)
    home = Path.home().resolve()
    if workroot == Path(workroot.anchor) or workroot == home:
        raise ValueError("executor work directory is too broad")
    _validate_private_path(workroot, expected_directory=True)
    _validate_private_path(
        workroot / WORKROOT_READY_FILE,
        expected_directory=False,
    )
    error = _workroot_health_error(workroot)
    if error is not None:
        raise ValueError("executor work directory is not writable")
    return workroot


def _validate_private_path(path: Path, *, expected_directory: bool) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise ValueError(f"required private path is unavailable: {path.name}") from exc
    expected_type = stat.S_ISDIR if expected_directory else stat.S_ISREG
    if stat.S_ISLNK(path_stat.st_mode) or not expected_type(path_stat.st_mode):
        raise ValueError(f"required private path has an unsafe type: {path.name}")
    if hasattr(os, "geteuid") and path_stat.st_uid != os.geteuid():
        raise ValueError(f"required private path has a different owner: {path.name}")
    if os.name != "nt" and stat.S_IMODE(path_stat.st_mode) & 0o077:
        raise ValueError(
            f"required private path permissions must be private: {path.name}"
        )


def _workroot_health_error(workroot: Path | None) -> str | None:
    if workroot is None:
        return None
    proof = workroot / f".executor-health-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            proof,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.write(descriptor, b"ok")
    except OSError as exc:
        return exc.__class__.__name__
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            proof.unlink(missing_ok=True)
        except OSError:
            pass
    return None


def _install_signal_handlers(server: ExecutorServer) -> dict[int, object]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, object] = {}

    def handle_shutdown(_signal_number, _frame) -> None:
        server.request_shutdown(cancel_active=True)

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        previous[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, handle_shutdown)
    return previous


def _restore_signal_handlers(previous: dict[int, object]) -> None:
    for signal_number, handler in previous.items():
        signal.signal(signal_number, handler)


def serve(
    host: str = "127.0.0.1",
    port: int = 0,
    store: ExecutionStore | None = None,
    runner_name: str = "babeldoc",
    *,
    token_file: str | Path | None = None,
    work_dir: str | Path | None = None,
    instance_id: str | None = None,
    parent_pid: int | None = None,
    parent_start_time: float | None = None,
) -> None:
    if not _is_loopback_host(host):
        raise ValueError("executor service must bind to a loopback host")
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    parent_pid, parent_start_time = _resolve_parent_identity(
        parent_pid,
        parent_start_time,
    )
    if store is None and runner_name == "babeldoc" and token_file is None:
        raise ValueError("babeldoc runner requires a private token file")
    if (
        store is None
        and runner_name == "fake"
        and os.environ.get(ALLOW_FAKE_RUNNER_ENV) != "1"
    ):
        raise ValueError("fake runner is disabled outside explicit test mode")

    workroot = _configure_workroot(
        work_dir,
        required=store is None,
    )
    if store is None:
        runner = _create_runner(runner_name)
        if isinstance(runner, MultiprocessExecutionRunner):
            runner.warmup()
        store = ExecutionStore(runner)
    token, generated_token = _load_token(token_file)
    server = ExecutorServer(
        (host, port),
        store,
        token=token,
        instance_id=instance_id,
        parent_pid=parent_pid,
        parent_start_time=parent_start_time,
        runner_name=runner_name,
        workroot=workroot,
    )
    previous_signal_handlers = _install_signal_handlers(server)
    ready = server.identity_payload()
    ready.update({"type": "ready"})
    if generated_token:
        ready["auth_token"] = token
    print(
        READY_PREFIX
        + json.dumps(
            ready,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )
    server.start_parent_watchdog()
    logger.info("starting executor on %s:%s", *server.server_address[:2])
    try:
        server.serve_forever()
    finally:
        server.request_shutdown(cancel_active=True)
        server.store.wait_until_idle(timeout_seconds=5.0)
        server.server_close()
        _restore_signal_handlers(previous_signal_handlers)


def _create_runner(runner_name: str):
    if runner_name == "babeldoc":
        from babeldoc.tools.executor.babeldoc_adapter import run_babeldoc_request

        runner = MultiprocessExecutionRunner(
            run_babeldoc_request,
            start_method="forkserver",
            preload_modules=(
                "babeldoc.tools.executor.runner",
                "babeldoc.tools.executor.babeldoc_adapter",
                "babeldoc.format.pdf.high_level",
                "babeldoc.docvision.rpc_doclayout8",
            ),
        )
        return runner
    if runner_name == "fake":
        return FakeExecutionRunner()
    raise ValueError(f"unknown executor runner: {runner_name}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Run HTTP executor.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--runner", choices=["babeldoc", "fake"], default="babeldoc")
    parser.add_argument("--token-file")
    parser.add_argument("--work-dir")
    parser.add_argument("--instance-id")
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--parent-start-time", type=float)
    args = parser.parse_args()
    serve(
        args.host,
        args.port,
        runner_name=args.runner,
        token_file=args.token_file,
        work_dir=args.work_dir,
        instance_id=args.instance_id,
        parent_pid=args.parent_pid,
        parent_start_time=args.parent_start_time,
    )
