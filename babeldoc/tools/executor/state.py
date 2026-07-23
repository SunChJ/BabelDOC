from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from babeldoc.tools.executor.protocol import ACTIVE_EXECUTION_STATUSES
from babeldoc.tools.executor.protocol import MAX_EVENT_LOG_SIZE
from babeldoc.tools.executor.protocol import MAX_EXECUTION_HISTORY_SIZE
from babeldoc.tools.executor.protocol import MAX_INITIAL_SEQUENCE
from babeldoc.tools.executor.protocol import MAX_SEQUENCE
from babeldoc.tools.executor.protocol import TERMINAL_EVENT_STATUS
from babeldoc.tools.executor.protocol import TERMINAL_EVENT_TYPES
from babeldoc.tools.executor.protocol import TERMINAL_EXECUTION_STATUSES
from babeldoc.tools.executor.protocol import EventEnvelope
from babeldoc.tools.executor.protocol import WorkerEvent
from babeldoc.tools.executor.runner import ExecutionRunner
from babeldoc.tools.executor.runner import UnavailableRunner

logger = logging.getLogger(__name__)


class ReplayGapError(Exception):
    pass


class CursorAheadError(Exception):
    pass


class ExecutionBusyError(Exception):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__("executor is busy")
        self.snapshot = snapshot


class ExecutionConflictError(Exception):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__("task_id already exists with a different request")
        self.snapshot = snapshot


class ExecutionNotFoundError(Exception):
    pass


@dataclass
class ExecutionRecord:
    execution_id: str
    task_id: str
    request: dict[str, Any]
    request_fingerprint: str
    initial_seq: int
    last_seq: int
    created_at: float
    finished_at: float | None = None
    status: str = "running"
    events: deque[EventEnvelope] = field(default_factory=deque)
    abort_event: threading.Event = field(default_factory=threading.Event)
    first_available_seq: int | None = None
    worker_finished: bool = False
    cancel_reason: str = "client_request"


class ExecutionStore:
    def __init__(
        self,
        runner: ExecutionRunner | None = None,
        max_event_log_size: int = MAX_EVENT_LOG_SIZE,
        max_execution_history_size: int = MAX_EXECUTION_HISTORY_SIZE,
    ):
        if max_event_log_size <= 0:
            raise ValueError("max_event_log_size must be positive")
        if max_execution_history_size <= 0:
            raise ValueError("max_execution_history_size must be positive")
        self._runner = runner or UnavailableRunner()
        self._max_event_log_size = max_event_log_size
        self._max_execution_history_size = max_execution_history_size
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._records: OrderedDict[str, ExecutionRecord] = OrderedDict()
        self._task_index: dict[str, str] = {}
        self._current_execution_id: str | None = None
        self._active_execution_id: str | None = None

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        request_fingerprint, request_copy = self._fingerprint_request(request)
        task_id = request_copy.get("task_id")
        self._validate_identifier(task_id, "task_id")

        with self._condition:
            existing = self._record_for_task_locked(task_id)
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise ExecutionConflictError(self._snapshot_locked(existing))
                return self._create_response_locked(existing, replayed=True)

            active = self._active_record_locked()
            if active is not None:
                logger.warning(
                    "executor create rejected because active execution exists: requested_task_id=%s active_task_id=%s active_execution_id=%s",
                    task_id,
                    active.task_id,
                    active.execution_id,
                )
                raise ExecutionBusyError(self._snapshot_locked(active))

            record = self._new_record(
                execution_id=str(uuid.uuid4()),
                task_id=task_id,
                request=request_copy,
                request_fingerprint=request_fingerprint,
            )
            self._register_record_locked(record, active=True)

        logger.info(
            "executor execution created: task_id=%s execution_id=%s initial_sequence=%s",
            record.task_id,
            record.execution_id,
            record.initial_seq,
        )
        try:
            self._start_worker(record)
        except Exception as exc:
            logger.exception(
                "executor worker thread failed to start: task_id=%s execution_id=%s",
                record.task_id,
                record.execution_id,
            )
            with self._condition:
                self._append_event_locked(
                    record,
                    WorkerEvent(
                        "error",
                        {
                            "code": "worker_start_failed",
                            "message": str(exc),
                            "message_for_user": None,
                            "details": {"exception_type": exc.__class__.__name__},
                        },
                    ),
                )
                self._finish_worker_locked(record)

        with self._lock:
            return self._create_response_locked(record, replayed=False)

    def _start_worker(self, record: ExecutionRecord) -> None:
        thread = threading.Thread(
            target=self._run,
            args=(record,),
            name=f"executor-{record.execution_id}",
            daemon=True,
        )
        thread.start()

    def cancel(
        self,
        execution_id: str,
        *,
        reason: str = "client_request",
    ) -> dict[str, Any]:
        if reason not in {"client_request", "service_shutdown", "parent_exit"}:
            raise ValueError("unsupported cancellation reason")
        with self._condition:
            record = self._require_record_locked(execution_id)
            if record.status == "running":
                logger.warning(
                    "executor execution cancellation requested: task_id=%s execution_id=%s",
                    record.task_id,
                    record.execution_id,
                )
                record.status = "cancelling"
                record.cancel_reason = reason
                record.abort_event.set()
                self._condition.notify_all()
            return self._snapshot_locked(record)

    def abort_current(self, *, reason: str = "client_request") -> None:
        with self._lock:
            execution_id = self._active_execution_id
        if execution_id is None:
            return
        self.cancel(execution_id, reason=reason)

    def begin_heavy_operation(self, operation_id: str) -> threading.Event:
        self._validate_identifier(operation_id, "operation_id")
        with self._condition:
            existing = self._records.get(operation_id)
            if existing is not None:
                raise ExecutionConflictError(self._snapshot_locked(existing))
            indexed = self._record_for_task_locked(operation_id)
            if indexed is not None:
                raise ExecutionConflictError(self._snapshot_locked(indexed))
            active = self._active_record_locked()
            if active is not None:
                raise ExecutionBusyError(self._snapshot_locked(active))

            record = self._new_record(
                execution_id=operation_id,
                task_id=operation_id,
                request={},
                request_fingerprint=hashlib.sha256(b"{}").hexdigest(),
            )
            self._register_record_locked(record, active=True)
            logger.info(
                "executor heavy operation started: operation_id=%s initial_sequence=%s",
                operation_id,
                record.initial_seq,
            )
            self._condition.notify_all()
            return record.abort_event

    def finish_heavy_operation(
        self,
        operation_id: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._condition:
            record = self._records.get(operation_id)
            if record is None or record.worker_finished:
                return
            if record.status == "cancelling":
                self._append_cancelled_event_locked(record)
            elif record.status == "running" and error_code is not None:
                self._append_event_locked(
                    record,
                    WorkerEvent(
                        "error",
                        {
                            "code": error_code,
                            "message": error_message or "heavy operation failed",
                            "message_for_user": None,
                            "details": {"operation_id": operation_id},
                        },
                    ),
                )
            elif record.status == "running":
                self._append_event_locked(
                    record,
                    WorkerEvent("result", {"operation_id": operation_id}),
                )
            self._finish_worker_locked(record)
            logger.info(
                "executor heavy operation finished: operation_id=%s status=%s",
                operation_id,
                record.status,
            )

    def replay(self, execution_id: str, after_seq: int) -> list[EventEnvelope]:
        with self._lock:
            record = self._require_record_locked(execution_id)
            self._raise_if_gap_locked(record, after_seq)
            return [event for event in record.events if event.sequence > after_seq]

    def stream(
        self,
        execution_id: str,
        after_seq: int,
        wait_seconds: float = 0.25,
        heartbeat_interval_seconds: float = 5.0,
    ) -> Iterable[EventEnvelope | None]:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        cursor = after_seq
        next_heartbeat_at = time.monotonic() + heartbeat_interval_seconds
        while True:
            heartbeat_due = False
            with self._condition:
                record = self._require_record_locked(execution_id)
                self._raise_if_gap_locked(record, cursor)
                pending = [event for event in record.events if event.sequence > cursor]
                status = record.status
                worker_finished = record.worker_finished
                if (
                    not pending
                    and status in ACTIVE_EXECUTION_STATUSES
                    and not worker_finished
                ):
                    now = time.monotonic()
                    if now >= next_heartbeat_at:
                        heartbeat_due = True
                    else:
                        self._condition.wait(
                            timeout=min(
                                wait_seconds,
                                next_heartbeat_at - now,
                            )
                        )
                        continue

            if heartbeat_due:
                yield None
                next_heartbeat_at = time.monotonic() + heartbeat_interval_seconds
                continue

            for event in pending:
                cursor = event.sequence
                yield event

            if pending:
                next_heartbeat_at = time.monotonic() + heartbeat_interval_seconds
            if pending and pending[-1].type in TERMINAL_EVENT_TYPES:
                return
            if not pending and (
                status in TERMINAL_EXECUTION_STATUSES or worker_finished
            ):
                return

    def snapshot(self, execution_id: str) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(self._require_record_locked(execution_id))

    def current_snapshot(self) -> dict[str, Any] | None:
        """Return the most recently created execution, including terminal ones."""
        with self._lock:
            if self._current_execution_id is None:
                return None
            record = self._records.get(self._current_execution_id)
            return self._snapshot_locked(record) if record is not None else None

    def active_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            record = self._active_record_locked()
            return self._snapshot_locked(record) if record is not None else None

    def wait_until_idle(self, timeout_seconds: float | None = None) -> bool:
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        deadline = (
            None if timeout_seconds is None else time.monotonic() + timeout_seconds
        )
        with self._condition:
            while self._active_execution_id is not None:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def _run(self, record: ExecutionRecord) -> None:
        def emit(event: WorkerEvent) -> None:
            self._append_event(record.execution_id, event)

        try:
            self._runner.run(record.request, emit, record.abort_event)
        except Exception as exc:
            with self._condition:
                if record.status == "running":
                    logger.exception(
                        "executor runner raised internal error: task_id=%s execution_id=%s",
                        record.task_id,
                        record.execution_id,
                    )
                    self._append_event_locked(
                        record,
                        WorkerEvent(
                            "error",
                            {
                                "code": "internal_error",
                                "message": str(exc),
                                "message_for_user": None,
                                "details": {"exception_type": exc.__class__.__name__},
                            },
                        ),
                    )
                elif record.status in TERMINAL_EXECUTION_STATUSES:
                    logger.exception(
                        "executor runner raised after terminal event: task_id=%s execution_id=%s status=%s",
                        record.task_id,
                        record.execution_id,
                        record.status,
                    )
        finally:
            with self._condition:
                if record.status == "cancelling":
                    self._append_cancelled_event_locked(record)
                elif record.status == "running":
                    logger.error(
                        "executor runner returned without terminal event: task_id=%s execution_id=%s",
                        record.task_id,
                        record.execution_id,
                    )
                    self._append_event_locked(
                        record,
                        WorkerEvent(
                            "error",
                            {
                                "code": "missing_terminal_event",
                                "message": (
                                    "executor runner returned without a terminal event"
                                ),
                                "message_for_user": None,
                                "details": {},
                            },
                        ),
                    )
                self._finish_worker_locked(record)

    def _append_event(self, execution_id: str, event: WorkerEvent) -> None:
        with self._condition:
            record = self._require_record_locked(execution_id)
            self._append_event_locked(record, event)
            self._condition.notify_all()

    def _append_event_locked(
        self,
        record: ExecutionRecord,
        event: WorkerEvent,
    ) -> None:
        if record.status in TERMINAL_EXECUTION_STATUSES:
            return
        if record.status == "cancelling" and event.type in {"result", "error"}:
            return
        if record.last_seq >= MAX_SEQUENCE:
            record.abort_event.set()
            raise OverflowError("event sequence exhausted")
        record.last_seq += 1
        if record.last_seq <= 0:
            record.abort_event.set()
            raise OverflowError("event sequence invariant violated")
        payload = (
            self._cancelled_payload(record, event.payload)
            if event.type == "cancelled"
            else event.payload
        )
        envelope = EventEnvelope(
            type=event.type,
            execution_id=record.execution_id,
            sequence=record.last_seq,
            emitted_at=time.time(),
            payload=payload,
        )
        record.events.append(envelope)
        if record.first_available_seq is None:
            record.first_available_seq = envelope.sequence
        while len(record.events) > self._max_event_log_size:
            record.events.popleft()
            record.first_available_seq = record.events[0].sequence

        terminal_status = TERMINAL_EVENT_STATUS.get(event.type)
        if terminal_status is None:
            return
        record.status = terminal_status
        record.finished_at = envelope.emitted_at
        if event.type == "result":
            logger.info(
                "executor terminal result emitted: task_id=%s execution_id=%s sequence=%s",
                record.task_id,
                record.execution_id,
                envelope.sequence,
            )
        else:
            logger.warning(
                "executor terminal event emitted: task_id=%s execution_id=%s sequence=%s type=%s code=%s message=%s",
                record.task_id,
                record.execution_id,
                envelope.sequence,
                event.type,
                event.payload.get("code"),
                event.payload.get("message"),
            )

    def _append_cancelled_event_locked(self, record: ExecutionRecord) -> None:
        self._append_event_locked(
            record,
            WorkerEvent("cancelled", {}),
        )

    @staticmethod
    def _cancelled_payload(
        record: ExecutionRecord,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        reason = (
            record.cancel_reason
            if record.status == "cancelling"
            else payload.get("reason", "client_request")
        )
        details = payload.get("details")
        return {
            "reason": reason,
            "code": "cancelled",
            "message": "execution cancelled",
            "message_for_user": payload.get("message_for_user"),
            "details": details if isinstance(details, dict) else {},
        }

    def _finish_worker_locked(self, record: ExecutionRecord) -> None:
        record.worker_finished = True
        record.request.clear()
        if self._active_execution_id == record.execution_id:
            self._active_execution_id = None
        self._prune_history_locked()
        self._condition.notify_all()

    def _new_record(
        self,
        *,
        execution_id: str,
        task_id: str,
        request: dict[str, Any],
        request_fingerprint: str,
    ) -> ExecutionRecord:
        initial_seq = secrets.randbelow(MAX_INITIAL_SEQUENCE) + 1
        return ExecutionRecord(
            execution_id=execution_id,
            task_id=task_id,
            request=request,
            request_fingerprint=request_fingerprint,
            initial_seq=initial_seq,
            last_seq=initial_seq,
            created_at=time.time(),
        )

    def _register_record_locked(
        self,
        record: ExecutionRecord,
        *,
        active: bool,
    ) -> None:
        self._records[record.execution_id] = record
        self._task_index[record.task_id] = record.execution_id
        self._current_execution_id = record.execution_id
        if active:
            self._active_execution_id = record.execution_id
        self._prune_history_locked()

    def _prune_history_locked(self) -> None:
        while len(self._records) > self._max_execution_history_size:
            execution_id, record = next(iter(self._records.items()))
            if execution_id == self._active_execution_id:
                return
            self._records.pop(execution_id)
            if self._task_index.get(record.task_id) == execution_id:
                self._task_index.pop(record.task_id, None)
            if self._current_execution_id == execution_id:
                self._current_execution_id = next(reversed(self._records), None)

    def _record_for_task_locked(self, task_id: str) -> ExecutionRecord | None:
        execution_id = self._task_index.get(task_id)
        if execution_id is None:
            return None
        record = self._records.get(execution_id)
        if record is None:
            self._task_index.pop(task_id, None)
        return record

    def _active_record_locked(self) -> ExecutionRecord | None:
        if self._active_execution_id is None:
            return None
        record = self._records.get(self._active_execution_id)
        if record is None:
            self._active_execution_id = None
        return record

    def _require_record_locked(self, execution_id: str) -> ExecutionRecord:
        record = self._records.get(execution_id)
        if record is None:
            raise ExecutionNotFoundError(execution_id)
        return record

    def _create_response_locked(
        self,
        record: ExecutionRecord,
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        return {
            "execution_id": record.execution_id,
            "status": record.status,
            "initial_sequence": record.initial_seq,
            "replayed": replayed,
        }

    def _snapshot_locked(self, record: ExecutionRecord) -> dict[str, Any]:
        return {
            "execution_id": record.execution_id,
            "task_id": record.task_id,
            "status": record.status,
            "initial_sequence": record.initial_seq,
            "first_available_sequence": record.first_available_seq,
            "last_sequence": record.last_seq,
            "worker_finished": record.worker_finished,
            "created_at": record.created_at,
            "finished_at": record.finished_at,
        }

    def _raise_if_gap_locked(self, record: ExecutionRecord, after_seq: int) -> None:
        if after_seq > record.last_seq:
            raise CursorAheadError("requested sequence is ahead of the execution")
        if record.first_available_seq is None:
            if after_seq < record.initial_seq:
                raise ReplayGapError("requested sequence is no longer available")
            return
        if after_seq < record.first_available_seq - 1:
            raise ReplayGapError("requested sequence is no longer available")

    @staticmethod
    def _fingerprint_request(
        request: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        try:
            canonical = json.dumps(
                request,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            request_copy = json.loads(canonical)
        except (TypeError, ValueError) as exc:
            raise ValueError("request must be JSON-serializable") from exc
        if not isinstance(request_copy, dict):  # pragma: no cover - defensive
            raise ValueError("request must be an object")
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return fingerprint, request_copy

    @staticmethod
    def _validate_identifier(value: Any, field_name: str) -> None:
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
            value,
        ):
            raise ValueError(
                f"{field_name} must be 1-128 URL-safe identifier characters"
            )
