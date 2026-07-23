from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

from babeldoc import __version__ as babeldoc_version
from babeldoc.format.pdf.translation_config import TranslateResult
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.translation_config import WatermarkOutputMode
from babeldoc.glossary import Glossary
from babeldoc.progress_monitor import ProgressMonitor
from babeldoc.tools.executor.layout_ir_cache import LayoutIRCache
from babeldoc.tools.executor.translator import ExecutorTranslator
from babeldoc.tools.executor.workroot import get_workroot
from babeldoc.tools.executor.workroot import relative_to_workroot
from babeldoc.tools.executor.workroot import resolve_dir
from babeldoc.tools.executor.workroot import resolve_file
from babeldoc.translator.translator import set_translate_rate_limiter

logger = logging.getLogger(__name__)
_TIMED_PHASES = (
    "launching",
    "parsing",
    "translating",
    "typesetting",
    "saving",
    "finalizing",
)


class ExecutionTelemetry:
    """Accumulate monotonic executor phase timings for progress and results."""

    def __init__(self, *, clock_ns=time.monotonic_ns):
        self._clock_ns = clock_ns
        self._started_at = clock_ns()
        self._phase_started_at = self._started_at
        self._phase = "launching"
        self._completed_ns = dict.fromkeys(_TIMED_PHASES, 0)

    def observe(
        self,
        event: dict[str, Any],
        config: TranslationConfig,
    ) -> dict[str, Any]:
        phase = _phase_for_stage(event.get("stage"))
        if event.get("type") in {
            "progress_start",
            "progress_update",
            "progress_end",
        }:
            self.transition(phase)
        payload = dict(event)
        payload["performance"] = self.snapshot(config)
        return payload

    def transition(self, phase: str) -> None:
        if phase == self._phase:
            return
        if phase not in {*_TIMED_PHASES, "completed"}:
            raise ValueError(f"unknown executor telemetry phase: {phase}")
        now = self._clock_ns()
        if self._phase in self._completed_ns:
            self._completed_ns[self._phase] += max(0, now - self._phase_started_at)
        self._phase = phase
        self._phase_started_at = now

    def snapshot(
        self,
        config: TranslationConfig | None = None,
    ) -> dict[str, Any]:
        now = self._clock_ns()
        timings = dict(self._completed_ns)
        if self._phase in timings:
            timings[self._phase] += max(0, now - self._phase_started_at)
        cache = getattr(config, "layout_ir_cache", None)
        return {
            "schema_version": 1,
            "phase": self._phase,
            "elapsed_milliseconds": _ns_to_milliseconds(now - self._started_at),
            "phase_timings_milliseconds": {
                phase: _ns_to_milliseconds(timings[phase]) for phase in _TIMED_PHASES
            },
            "layout_ir_cache_status": getattr(cache, "status", "disabled"),
        }

    def finish(self, config: TranslationConfig) -> dict[str, Any]:
        self.transition("completed")
        return self.snapshot(config)


def run_babeldoc_request(request: dict[str, Any], progress_send, cancel_recv) -> None:
    telemetry = ExecutionTelemetry()
    cancel_state = _CancelState()
    config: TranslationConfig | None = None
    stop_cancel_watcher: threading.Event | None = None
    cancel_watcher: threading.Thread | None = None
    task_id = _task_id(request)

    def emit(event_type: str, payload: dict[str, Any]) -> None:
        progress_send.send({"type": event_type, "payload": payload})

    try:
        config_started_at = time.monotonic()
        config = build_translation_config(request, task_id=task_id)
        config_elapsed = time.monotonic() - config_started_at
        logger.info(
            "BabelDOC execution started: task_id=%s input=%s output_dir=%s pages=%s lang_in=%s lang_out=%s model=%s config_elapsed=%.3fs",
            task_id,
            getattr(config, "input_file", None),
            getattr(config, "output_dir", None),
            getattr(config, "pages", None),
            getattr(config, "lang_in", None),
            getattr(config, "lang_out", None),
            getattr(config, "model", None),
            config_elapsed,
        )
        cancel_state.attach_config(config)
        stop_cancel_watcher = threading.Event()
        cancel_watcher = threading.Thread(
            target=_watch_cancel_pipe,
            args=(cancel_recv, cancel_state, stop_cancel_watcher),
            name="executor-cancel-watch",
            daemon=True,
        )
        cancel_watcher.start()

        result = _run_async_translate(config, emit, telemetry)
        if cancel_state.cancel_requested:
            emit("cancelled", {"reason": "client_request"})
        else:
            logger.info("BabelDOC execution finished: task_id=%s", task_id)
            payload = translate_result_to_payload(result, config)
            payload["performance"] = telemetry.finish(config)
            emit("result", payload)
    except asyncio.CancelledError:
        emit("cancelled", {"reason": "client_request"})
    except Exception as exc:
        if cancel_state.cancel_requested:
            emit("cancelled", {"reason": "client_request"})
        else:
            logger.exception("BabelDOC execution failed: task_id=%s", task_id)
            user_message = (
                exc.message_for_user if isinstance(exc, BabelDocReportedError) else None
            )
            emit(
                "error",
                {
                    "code": _error_code(exc),
                    "message": _safe_message(exc),
                    "message_for_user": user_message,
                    "details": {"exception_type": exc.__class__.__name__},
                    "performance": telemetry.snapshot(config),
                },
            )
    finally:
        if stop_cancel_watcher is not None:
            stop_cancel_watcher.set()
        progress_send.send(None)
        if cancel_watcher is not None:
            cancel_watcher.join(timeout=1)


class _CancelState:
    def __init__(self):
        self._lock = threading.Lock()
        self._config: TranslationConfig | None = None
        self._cancel_requested = False

    def request_cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            config = self._config
        if config is not None:
            config.cancel_translation()

    def reapply_cancel(self) -> None:
        with self._lock:
            if not self._cancel_requested:
                return
            config = self._config
        if config is not None:
            config.cancel_translation()

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def attach_config(self, config: TranslationConfig) -> None:
        with self._lock:
            self._config = config
            cancel_requested = self._cancel_requested
        if cancel_requested:
            config.cancel_translation()


def _watch_cancel_pipe(cancel_recv, cancel_state: _CancelState, stop_event) -> None:
    parent_lost = False
    while not stop_event.is_set():
        try:
            if cancel_recv.poll(0.05):
                cancel_recv.recv()
                cancel_state.request_cancel()
                break
        except (BrokenPipeError, EOFError, OSError):
            parent_lost = True
            cancel_state.request_cancel()
            break
    hard_exit_at = time.monotonic() + 2.0 if parent_lost else None
    while True:
        if stop_event.wait(0.05):
            if parent_lost:
                _hard_exit_after_parent_loss()
            return
        cancel_state.reapply_cancel()
        if hard_exit_at is not None and time.monotonic() >= hard_exit_at:
            _hard_exit_after_parent_loss()


def _hard_exit_after_parent_loss() -> None:
    process_id = os.getpid()
    kill_signal = getattr(signal, "SIGKILL", None)
    if kill_signal is not None and hasattr(os, "getpgrp") and hasattr(os, "killpg"):
        try:
            if os.getpgrp() == process_id:
                os.killpg(process_id, kill_signal)
        except OSError:
            logger.exception(
                "failed to kill executor worker process group after parent loss"
            )
    os._exit(130)


def build_translation_config(
    request: dict[str, Any],
    *,
    task_id: str | None = None,
) -> TranslationConfig:
    total_started_at = time.monotonic()
    timing: dict[str, float] = {}

    def mark(name: str, started_at: float) -> None:
        timing[name] = time.monotonic() - started_at

    workroot = get_workroot()
    paths = _required_object(request, "paths")
    translation = _required_object(request, "translation_config")
    runtime_limits = _required_object(request, "runtime_limits")
    gateways = _required_object(request, "gateways")
    assets = _optional_object(request, "assets")
    metadata = _optional_object(request, "metadata")
    no_dual = _required_bool(translation, "no_dual")
    no_mono = _required_bool(translation, "no_mono")
    if no_dual and no_mono:
        raise ValueError("no_dual and no_mono cannot both be true")

    started_at = time.monotonic()
    input_file = resolve_file(workroot, _required_str(paths, "input_file"))
    output_dir = resolve_dir(workroot, _required_str(paths, "output_dir"), create=True)
    working_dir = resolve_dir(
        workroot,
        _optional_str(paths, "working_dir") or _required_str(paths, "output_dir"),
        create=True,
    )
    mark("paths", started_at)

    started_at = time.monotonic()
    qps = _required_positive_int(runtime_limits, "qps")
    report_interval = _required_positive_number(
        runtime_limits,
        "report_interval_seconds",
    )
    set_translate_rate_limiter(qps)

    max_pages_per_part = _required_positive_int(
        runtime_limits,
        "max_pages_per_part",
    )
    split_strategy = TranslationConfig.create_max_pages_per_part_split_strategy(
        max_pages_per_part
    )
    mark("limits", started_at)

    started_at = time.monotonic()
    translator = _create_translator(
        _required_object(gateways, "main_llm"),
        translation,
    )
    mark("main_translator", started_at)

    started_at = time.monotonic()
    term_translator = _create_translator(
        _required_object(gateways, "ate_llm"),
        translation,
    )
    mark("term_translator", started_at)

    started_at = time.monotonic()
    doc_layout_model = _create_doc_layout_model(_required_object(gateways, "layout"))
    mark("layout_model", started_at)

    primary_font_family = _optional_str(translation, "primary_font_family")
    if primary_font_family == "none":
        primary_font_family = None

    started_at = time.monotonic()
    glossaries = _load_glossaries(
        workroot,
        assets,
        _required_str(translation, "lang_out"),
    )
    mark("glossaries", started_at)

    started_at = time.monotonic()
    layout_ir_cache = _create_layout_ir_cache(
        workroot,
        assets,
        max_pages_per_part=max_pages_per_part,
    )
    mark("layout_ir_cache", started_at)

    started_at = time.monotonic()
    config = TranslationConfig(
        input_file=str(input_file),
        output_dir=str(output_dir),
        working_dir=str(working_dir),
        translator=translator,
        term_extraction_translator=term_translator,
        debug=_required_bool(translation, "debug"),
        lang_in=_required_str(translation, "lang_in"),
        lang_out=_required_str(translation, "lang_out"),
        pages=_optional_str(translation, "pages"),
        no_dual=no_dual,
        no_mono=no_mono,
        qps=qps,
        doc_layout_model=doc_layout_model,
        skip_clean=_required_bool(translation, "skip_clean"),
        dual_translate_first=_required_bool(translation, "dual_translate_first"),
        disable_rich_text_translate=_required_bool(
            translation, "disable_rich_text_translate"
        ),
        enhance_compatibility=False,
        use_side_by_side_dual=_required_bool(translation, "use_side_by_side_dual"),
        use_alternating_pages_dual=_required_bool(
            translation, "use_alternating_pages_dual"
        ),
        report_interval=report_interval,
        progress_monitor=ProgressMonitor(
            [("translate", 1.0)],
            report_interval=report_interval,
        ),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        split_strategy=split_strategy,
        skip_scanned_detection=_required_bool(translation, "skip_scanned_detection"),
        ocr_workaround=_required_bool(translation, "ocr_workaround"),
        custom_system_prompt=_optional_str(translation, "custom_system_prompt"),
        glossaries=glossaries,
        pool_max_workers=_required_positive_int(
            runtime_limits,
            "pool_max_workers",
        ),
        auto_extract_glossary=_required_bool(translation, "auto_extract_glossary"),
        auto_enable_ocr_workaround=_required_bool(
            translation, "auto_enable_ocr_workaround"
        ),
        primary_font_family=primary_font_family,
        only_include_translated_page=_required_bool(
            translation, "only_include_translated_page"
        ),
        save_auto_extracted_glossary=True,
        merge_alternating_line_numbers=_required_bool(
            translation, "merge_alternating_line_numbers"
        ),
        remove_non_formula_lines=_required_bool(
            translation, "remove_non_formula_lines"
        ),
        metadata_extra_data=_optional_str(metadata, "metadata_extra_data"),
        term_pool_max_workers=_required_positive_int(
            runtime_limits,
            "term_pool_max_workers",
        ),
    )
    config.layout_ir_cache = layout_ir_cache
    mark("translation_config", started_at)

    started_at = time.monotonic()
    getattr(doc_layout_model, "init_font_mapper", lambda _config: None)(config)
    mark("font_mapper", started_at)

    total_elapsed = time.monotonic() - total_started_at
    logger.info(
        "BabelDOC config timing: task_id=%s total=%.3fs paths=%.3fs limits=%.3fs main_translator=%.3fs term_translator=%.3fs layout_model=%.3fs glossaries=%.3fs layout_ir_cache=%.3fs translation_config=%.3fs font_mapper=%.3fs glossary_count=%s",
        task_id or _task_id(request),
        total_elapsed,
        timing.get("paths", 0.0),
        timing.get("limits", 0.0),
        timing.get("main_translator", 0.0),
        timing.get("term_translator", 0.0),
        timing.get("layout_model", 0.0),
        timing.get("glossaries", 0.0),
        timing.get("layout_ir_cache", 0.0),
        timing.get("translation_config", 0.0),
        timing.get("font_mapper", 0.0),
        len(glossaries),
    )
    return config


def _task_id(request: dict[str, Any]) -> str:
    value = request.get("task_id")
    return value if isinstance(value, str) and value else "unknown"


def translate_result_to_payload(
    result: TranslateResult, config: TranslationConfig
) -> dict[str, Any]:
    workroot = get_workroot()
    output_dir = resolve_dir(workroot, str(config.output_dir))
    if config.no_mono and config.no_dual:
        raise ValueError("no_dual and no_mono cannot both be true")
    pdf_paths = {
        "mono_pdf": result.mono_pdf_path,
        "dual_pdf": result.dual_pdf_path,
        "mono_no_watermark_pdf": result.no_watermark_mono_pdf_path,
        "dual_no_watermark_pdf": result.no_watermark_dual_pdf_path,
    }
    files = {
        key: _validated_pdf_result_path(workroot, output_dir, path, key)
        for key, path in pdf_paths.items()
        if path is not None
    }
    if not files:
        raise ValueError("BabelDOC result did not contain a PDF")
    if not config.no_mono and not {
        "mono_pdf",
        "mono_no_watermark_pdf",
    }.intersection(files):
        raise ValueError("BabelDOC result did not contain the requested mono PDF")
    if not config.no_dual and not {
        "dual_pdf",
        "dual_no_watermark_pdf",
    }.intersection(files):
        raise ValueError("BabelDOC result did not contain the requested dual PDF")
    glossary_path = relative_to_workroot(
        workroot,
        result.auto_extracted_glossary_path,
    )
    if glossary_path:
        glossary_file = resolve_file(workroot, glossary_path)
        _require_inside_output_dir(glossary_file, output_dir, "auto glossary")
        if glossary_file.suffix.lower() != ".csv":
            raise ValueError("auto_extracted_glossary_csv is not a CSV")
        files["auto_extracted_glossary_csv"] = glossary_path
    return {
        "files": files,
        "metrics": {
            "time_consume_seconds": _number_or_zero(
                getattr(result, "total_seconds", None)
            ),
            "peak_memory_usage": _number_or_zero(
                getattr(result, "peak_memory_usage", None)
            ),
            "pdf_total_char_count": int(
                getattr(result, "total_valid_character_count", 0) or 0
            ),
            "pdf_total_char_token_count": int(
                getattr(result, "total_valid_text_token_count", 0) or 0
            ),
        },
        "pages": _pages_to_string(config),
    }


def _validated_pdf_result_path(
    workroot: Path,
    output_dir: Path,
    value: str | Path,
    field_name: str,
) -> str:
    try:
        path = Path(value).resolve(strict=True)
        relative_path = relative_to_workroot(workroot, path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{field_name} is outside the workroot or missing") from exc
    if relative_path is None or not path.is_file():
        raise ValueError(f"{field_name} is not a regular file")
    _require_inside_output_dir(path, output_dir, field_name)
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"{field_name} is not a PDF")
    try:
        with path.open("rb") as handle:
            header = handle.read(1024)
    except OSError as exc:
        raise ValueError(f"{field_name} is not readable") from exc
    if b"%PDF-" not in header:
        raise ValueError(f"{field_name} does not contain a PDF header")
    try:
        import pymupdf

        with pymupdf.open(path) as document:
            if document.page_count < 1:
                raise ValueError(f"{field_name} contains no pages")
            document.load_page(0)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{field_name} is not a readable PDF") from exc
    return relative_path


def _require_inside_output_dir(path: Path, output_dir: Path, field_name: str) -> None:
    try:
        path.relative_to(output_dir)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} is outside the execution output directory"
        ) from exc


def _run_async_translate(
    config: TranslationConfig,
    emit,
    telemetry: ExecutionTelemetry,
) -> TranslateResult:
    from babeldoc.format.pdf import high_level

    emit(
        "progress",
        telemetry.observe(
            {
                "type": "babeldoc_version",
                "version": babeldoc_version,
            },
            config,
        ),
    )

    async def run() -> TranslateResult:
        async for event in high_level.async_translate(config):
            event_type = event.get("type")
            if event_type == "finish":
                result = event.get("translate_result")
                if isinstance(result, TranslateResult):
                    telemetry.transition("finalizing")
                    return result
                raise ValueError("BabelDOC finish event did not contain result")
            if event_type == "error":
                raise BabelDocReportedError(
                    str(event.get("error")),
                    event.get("message_for_user"),
                )
            emit("progress", telemetry.observe(dict(event), config))
        raise RuntimeError("BabelDOC async_translate ended without finish event")

    return asyncio.run(run())


def _create_layout_ir_cache(
    workroot: Path,
    assets: dict[str, Any],
    *,
    max_pages_per_part: int,
) -> LayoutIRCache | None:
    raw_options = assets.get("layout_ir_cache")
    if raw_options is None:
        return None
    if not isinstance(raw_options, dict):
        raise ValueError("assets.layout_ir_cache must be an object")
    enabled = raw_options.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("assets.layout_ir_cache.enabled must be a boolean")
    if not enabled:
        return None
    cache_root = resolve_dir(
        workroot,
        ".cache/layout-ir",
        create=True,
    )
    cache_root.chmod(0o700)
    return LayoutIRCache(
        cache_root,
        max_pages_per_part=max_pages_per_part,
    )


def _phase_for_stage(value: object) -> str:
    stage = value.lower() if isinstance(value, str) else ""
    if "translate paragraph" in stage or "term extraction" in stage:
        return "translating"
    if (
        "typesetting" in stage
        or "add fonts" in stage
        or "drawing instructions" in stage
    ):
        return "typesetting"
    if "subset font" in stage or "save pdf" in stage:
        return "saving"
    return "parsing"


def _ns_to_milliseconds(value: int) -> int:
    return max(0, value) // 1_000_000


class BabelDocReportedError(RuntimeError):
    def __init__(self, message: str, message_for_user: Any):
        super().__init__(message)
        self.message_for_user = (
            message_for_user if isinstance(message_for_user, str) else None
        )


def _create_translator(gateway: dict[str, Any], translation: dict[str, Any]):
    return ExecutorTranslator(
        lang_in=_required_str(translation, "lang_in"),
        lang_out=_required_str(translation, "lang_out"),
        model=_required_str(gateway, "model"),
        base_url=_required_str(gateway, "base_url"),
        api_key=_required_str(gateway, "api_key"),
    )


def _create_doc_layout_model(layout: dict[str, Any]):
    adapter = _required_str(layout, "adapter")
    if adapter != "rpc_doclayout8":
        raise ValueError("gateways.layout.adapter must be rpc_doclayout8")

    from babeldoc.docvision.rpc_doclayout8 import RpcDocLayoutModel

    return RpcDocLayoutModel(
        host=_required_str(layout, "base_url"),
        requires_line_extraction=_required_bool(layout, "requires_line_extraction"),
    )


def _load_glossaries(
    workroot: Path,
    assets: dict[str, Any],
    lang_out: str,
) -> list[Glossary]:
    glossaries = assets.get("glossaries") or []
    if not isinstance(glossaries, list):
        raise ValueError("assets.glossaries must be an array")
    loaded: list[Glossary] = []
    for item in glossaries:
        if not isinstance(item, dict):
            raise ValueError("glossary asset must be an object")
        path = resolve_file(workroot, _required_str(item, "path"))
        glossary = Glossary.from_csv(path, lang_out)
        name = _optional_str(item, "name")
        if name is not None:
            glossary.name = name
        loaded.append(glossary)
    return loaded


def _pages_to_string(config: TranslationConfig) -> str | None:
    pages = getattr(config, "pages", None)
    if pages:
        return str(pages)
    page_ranges = getattr(config, "page_ranges", None)
    if not page_ranges:
        return None
    return ",".join(f"{start}-{end}" for start, end in page_ranges)


def _error_code(exc: Exception) -> str:
    name = exc.__class__.__name__.lower()
    if "scanned" in name:
        return "babeldoc_scanned_pdf"
    if isinstance(exc, TimeoutError):
        return "subprocess_timeout"
    if isinstance(exc, FileNotFoundError | ValueError):
        return "invalid_output"
    return "babeldoc_failed"


def _safe_message(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return "translation failed"
    return text.splitlines()[0][:500]


def _number_or_zero(value: Any) -> float | int:
    if isinstance(value, int | float):
        return value
    return 0


def _required_object(root: dict[str, Any], key: str) -> dict[str, Any]:
    value = root.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _optional_object(root: dict[str, Any], key: str) -> dict[str, Any]:
    value = root.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _required_str(root: dict[str, Any], key: str) -> str:
    value = root.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_str(root: dict[str, Any], key: str) -> str | None:
    value = root.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _required_bool(root: dict[str, Any], key: str) -> bool:
    value = root.get(key)
    if isinstance(value, bool):
        return value
    raise ValueError(f"{key} must be a boolean")


def _required_positive_int(root: dict[str, Any], key: str) -> int:
    value = root.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise ValueError(f"{key} must be a positive integer")


def _required_positive_number(root: dict[str, Any], key: str) -> float:
    value = root.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    raise ValueError(f"{key} must be a positive number")
