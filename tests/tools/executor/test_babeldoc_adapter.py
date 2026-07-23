from __future__ import annotations

import asyncio
import secrets
import signal
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pymupdf
import pytest
from babeldoc.format.pdf.translation_config import TranslateResult
from babeldoc.tools.executor import babeldoc_adapter
from babeldoc.tools.executor.babeldoc_adapter import ExecutionTelemetry
from babeldoc.tools.executor.babeldoc_adapter import build_translation_config
from babeldoc.tools.executor.babeldoc_adapter import run_babeldoc_request
from babeldoc.tools.executor.babeldoc_adapter import translate_result_to_payload
from babeldoc.tools.executor.workroot import WORKROOT_ENV


class _ProgressSender:
    def __init__(self) -> None:
        self.items: list[Any] = []

    def send(self, item: Any) -> None:
        self.items.append(item)


class _NeverCancelReceiver:
    def poll(self, timeout: float) -> bool:
        time.sleep(min(timeout, 0.001))
        return False


class _LostParentReceiver:
    def poll(self, _timeout: float) -> bool:
        return True

    def recv(self) -> None:
        raise EOFError


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        input_file="input.pdf",
        output_dir="output",
        no_dual=True,
        no_mono=False,
        pages=None,
        page_ranges=None,
        lang_in="en",
        lang_out="zh-CN",
        model="test",
    )


def _result(
    mono: Path | None = None,
    dual: Path | None = None,
    glossary: Path | None = None,
) -> TranslateResult:
    return TranslateResult(
        mono_pdf_path=mono,
        dual_pdf_path=dual,
        auto_extracted_glossary_path=glossary,
    )


def _write_pdf(path: Path) -> None:
    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()


def _complete_execution_request(api_key: str) -> dict[str, Any]:
    gateway = {
        "model": "gloss-provider",
        "base_url": "http://127.0.0.1:1/v1",
        "api_key": api_key,
    }
    return {
        "task_id": "contract-fixture",
        "paths": {
            "input_file": "input.pdf",
            "output_dir": "output",
            "working_dir": "working",
        },
        "translation_config": {
            "debug": False,
            "lang_in": "en",
            "lang_out": "zh-CN",
            "pages": None,
            "no_dual": True,
            "no_mono": False,
            "skip_clean": False,
            "dual_translate_first": False,
            "disable_rich_text_translate": True,
            "use_side_by_side_dual": False,
            "use_alternating_pages_dual": False,
            "skip_scanned_detection": True,
            "ocr_workaround": False,
            "custom_system_prompt": None,
            "primary_font_family": None,
            "auto_extract_glossary": False,
            "auto_enable_ocr_workaround": False,
            "only_include_translated_page": False,
            "merge_alternating_line_numbers": True,
            "remove_non_formula_lines": False,
        },
        "runtime_limits": {
            "qps": 4,
            "report_interval_seconds": 0.5,
            "max_pages_per_part": 50,
            "pool_max_workers": 4,
            "term_pool_max_workers": 4,
        },
        "gateways": {
            "main_llm": dict(gateway),
            "ate_llm": dict(gateway),
            "layout": {
                "adapter": "rpc_doclayout8",
                "base_url": "http://127.0.0.1:2",
                "requires_line_extraction": False,
            },
        },
        "assets": {
            "glossaries": [],
            "layout_ir_cache": {"enabled": True},
        },
        "metadata": {"metadata_extra_data": None},
    }


def test_result_payload_requires_a_real_pdf_inside_workroot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    pdf = workroot / "output" / "translated.pdf"
    pdf.parent.mkdir()
    _write_pdf(pdf)
    monkeypatch.setenv(WORKROOT_ENV, str(workroot))

    payload = translate_result_to_payload(_result(mono=pdf), _config())

    assert payload["files"] == {
        "mono_pdf": "output/translated.pdf",
        "mono_no_watermark_pdf": "output/translated.pdf",
    }


def test_complete_execution_request_builds_translation_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    (workroot / "input.pdf").write_bytes(b"fixture")
    monkeypatch.setenv(WORKROOT_ENV, str(workroot))
    api_key = secrets.token_urlsafe(32)

    config = build_translation_config(
        _complete_execution_request(api_key),
        task_id="contract-fixture",
    )
    try:
        assert config.input_file == str(workroot / "input.pdf")
        assert config.output_dir == str(workroot / "output")
        assert config.lang_in == "en"
        assert config.lang_out == "zh-CN"
        assert config.no_dual is True
        assert config.no_mono is False
        assert config.qps == 4
        assert config.pool_max_workers == 4
        assert config.term_pool_max_workers == 4
        assert config.skip_scanned_detection is True
        assert config.disable_rich_text_translate is True
        assert config.translator.api_key == api_key
        assert config.doc_layout_model.host == "http://127.0.0.1:2"
        assert config.layout_ir_cache is not None
        assert config.layout_ir_cache.root == workroot / ".cache" / "layout-ir"
        assert config.layout_ir_cache.root.stat().st_mode & 0o777 == 0o700
    finally:
        config.translator._client.close()
        config.term_extraction_translator._client.close()


@pytest.mark.parametrize(
    "invalid_kind",
    ["missing", "not_pdf", "corrupt_pdf", "outside_output", "outside_workroot"],
)
def test_result_payload_rejects_invalid_pdf_outputs(
    invalid_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    output_dir = workroot / "output"
    output_dir.mkdir()
    monkeypatch.setenv(WORKROOT_ENV, str(workroot))

    if invalid_kind == "missing":
        invalid = output_dir / "missing.pdf"
    elif invalid_kind == "not_pdf":
        invalid = output_dir / "not-a-pdf.pdf"
        invalid.write_text("plain text", encoding="utf-8")
    elif invalid_kind == "corrupt_pdf":
        invalid = output_dir / "corrupt.pdf"
        invalid.write_bytes(b"%PDF-1.7\n%%EOF\n")
    elif invalid_kind == "outside_output":
        invalid = workroot / "stale.pdf"
        _write_pdf(invalid)
    else:
        invalid = tmp_path / "outside.pdf"
        _write_pdf(invalid)

    with pytest.raises(ValueError):
        translate_result_to_payload(_result(mono=invalid), _config())


def test_result_payload_rejects_empty_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(WORKROOT_ENV, str(tmp_path))
    (tmp_path / "output").mkdir()

    with pytest.raises(ValueError, match="did not contain a PDF"):
        translate_result_to_payload(_result(), _config())


def test_result_payload_requires_each_requested_pdf_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    output_dir = workroot / "output"
    output_dir.mkdir(parents=True)
    mono = output_dir / "mono.pdf"
    dual = output_dir / "dual.pdf"
    _write_pdf(mono)
    _write_pdf(dual)
    monkeypatch.setenv(WORKROOT_ENV, str(workroot))

    both_modes = _config()
    both_modes.no_dual = False
    with pytest.raises(ValueError, match="requested dual PDF"):
        translate_result_to_payload(_result(mono=mono), both_modes)

    dual_only = _config()
    dual_only.no_dual = False
    dual_only.no_mono = True
    payload = translate_result_to_payload(_result(dual=dual), dual_only)
    assert set(payload["files"]) == {"dual_pdf", "dual_no_watermark_pdf"}

    with pytest.raises(ValueError, match="requested mono PDF"):
        translate_result_to_payload(_result(dual=dual), _config())


def test_result_payload_rejects_disabled_mono_and_dual_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    pdf = output_dir / "translated.pdf"
    _write_pdf(pdf)
    monkeypatch.setenv(WORKROOT_ENV, str(tmp_path))
    config = _config()
    config.no_mono = True
    config.no_dual = True

    with pytest.raises(ValueError, match="cannot both be true"):
        translate_result_to_payload(_result(mono=pdf), config)


def test_result_payload_rejects_glossary_outside_execution_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    pdf = output_dir / "translated.pdf"
    _write_pdf(pdf)
    stale_glossary = tmp_path / "stale.csv"
    stale_glossary.write_text("source,target\n", encoding="utf-8")
    monkeypatch.setenv(WORKROOT_ENV, str(tmp_path))

    with pytest.raises(ValueError, match="outside the execution output directory"):
        translate_result_to_payload(
            _result(mono=pdf, glossary=stale_glossary),
            _config(),
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        (field_name, invalid_value)
        for field_name in (
            "qps",
            "max_pages_per_part",
            "pool_max_workers",
            "term_pool_max_workers",
        )
        for invalid_value in (True, 0, -1)
    ],
)
def test_runtime_integer_limits_must_be_positive_non_boolean(
    field_name: str,
    invalid_value: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    (workroot / "input.pdf").write_bytes(b"fixture")
    monkeypatch.setenv(WORKROOT_ENV, str(workroot))
    monkeypatch.setattr(
        babeldoc_adapter,
        "_create_translator",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        babeldoc_adapter,
        "_create_doc_layout_model",
        lambda *_args, **_kwargs: SimpleNamespace(
            init_font_mapper=lambda _config: None
        ),
    )
    request = _complete_execution_request(secrets.token_urlsafe(32))
    request["runtime_limits"][field_name] = invalid_value

    with pytest.raises(ValueError, match=f"{field_name} must be a positive integer"):
        build_translation_config(request)


@pytest.mark.parametrize("invalid_value", [True, 0, -0.1])
def test_report_interval_must_be_positive_non_boolean(
    invalid_value: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    (workroot / "input.pdf").write_bytes(b"fixture")
    monkeypatch.setenv(WORKROOT_ENV, str(workroot))
    request = _complete_execution_request(secrets.token_urlsafe(32))
    request["runtime_limits"]["report_interval_seconds"] = invalid_value

    with pytest.raises(
        ValueError,
        match="report_interval_seconds must be a positive number",
    ):
        build_translation_config(request)


def test_build_config_rejects_disabling_both_output_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    (workroot / "input.pdf").write_bytes(b"fixture")
    monkeypatch.setenv(WORKROOT_ENV, str(workroot))
    request = _complete_execution_request(secrets.token_urlsafe(32))
    request["translation_config"]["no_dual"] = True
    request["translation_config"]["no_mono"] = True

    with pytest.raises(ValueError, match="cannot both be true"):
        build_translation_config(request)


class _HardExitCalled(BaseException):
    pass


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL")
    or not hasattr(babeldoc_adapter.os, "getpgrp")
    or not hasattr(babeldoc_adapter.os, "killpg"),
    reason="process-group SIGKILL is unavailable",
)
def test_parent_loss_kills_only_a_private_worker_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(babeldoc_adapter.os, "getpid", lambda: 123)
    monkeypatch.setattr(babeldoc_adapter.os, "getpgrp", lambda: 123)
    monkeypatch.setattr(
        babeldoc_adapter.os,
        "killpg",
        lambda process_group, signal_number: calls.append(
            ("killpg", process_group, signal_number)
        ),
    )

    def hard_exit(status: int) -> None:
        calls.append(("exit", status))
        raise _HardExitCalled

    monkeypatch.setattr(babeldoc_adapter.os, "_exit", hard_exit)

    with pytest.raises(_HardExitCalled):
        babeldoc_adapter._hard_exit_after_parent_loss()

    assert calls == [("killpg", 123, signal.SIGKILL), ("exit", 130)]


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL")
    or not hasattr(babeldoc_adapter.os, "getpgrp")
    or not hasattr(babeldoc_adapter.os, "killpg"),
    reason="process-group SIGKILL is unavailable",
)
def test_parent_loss_does_not_signal_a_shared_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(babeldoc_adapter.os, "getpid", lambda: 123)
    monkeypatch.setattr(babeldoc_adapter.os, "getpgrp", lambda: 456)
    monkeypatch.setattr(
        babeldoc_adapter.os,
        "killpg",
        lambda *_args: calls.append(("killpg",)),
    )

    def hard_exit(status: int) -> None:
        calls.append(("exit", status))
        raise _HardExitCalled

    monkeypatch.setattr(babeldoc_adapter.os, "_exit", hard_exit)

    with pytest.raises(_HardExitCalled):
        babeldoc_adapter._hard_exit_after_parent_loss()

    assert calls == [("exit", 130)]


def test_adapter_emits_cancelled_terminal_for_async_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = _ProgressSender()
    monkeypatch.setattr(
        babeldoc_adapter,
        "build_translation_config",
        lambda _request, **_kwargs: _config(),
    )

    def cancel(_config, _emit, _telemetry):
        raise asyncio.CancelledError

    monkeypatch.setattr(babeldoc_adapter, "_run_async_translate", cancel)

    run_babeldoc_request(
        {"task_id": "cancelled"},
        sender,
        _NeverCancelReceiver(),
    )

    assert sender.items == [
        {"type": "cancelled", "payload": {"reason": "client_request"}},
        None,
    ]


def test_cancel_state_reapplies_cancel_after_monitor_replacement() -> None:
    class Config:
        generation = 0

        def __init__(self) -> None:
            self.cancelled_generations: list[int] = []

        def cancel_translation(self) -> None:
            self.cancelled_generations.append(self.generation)

    config = Config()
    state = babeldoc_adapter._CancelState()
    state.attach_config(config)  # type: ignore[arg-type]
    state.request_cancel()
    config.generation = 1
    state.reapply_cancel()

    assert config.cancelled_generations == [0, 1]


def test_parent_pipe_eof_requests_cooperative_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = threading.Event()
    hard_exit_requested = threading.Event()
    config = _config()
    config.cancel_translation = cancelled.set
    sender = _ProgressSender()
    monkeypatch.setattr(
        babeldoc_adapter,
        "build_translation_config",
        lambda _request, **_kwargs: config,
    )

    def wait_for_cancel(_config, _emit, _telemetry):
        assert cancelled.wait(timeout=1)
        raise asyncio.CancelledError

    monkeypatch.setattr(babeldoc_adapter, "_run_async_translate", wait_for_cancel)
    monkeypatch.setattr(
        babeldoc_adapter,
        "_hard_exit_after_parent_loss",
        hard_exit_requested.set,
    )

    run_babeldoc_request(
        {"task_id": "parent-lost"},
        sender,
        _LostParentReceiver(),
    )

    assert sender.items[0] == {
        "type": "cancelled",
        "payload": {"reason": "client_request"},
    }
    assert hard_exit_requested.wait(timeout=1)


def test_execution_telemetry_accumulates_repeated_phases() -> None:
    class Clock:
        now = 0

        def __call__(self) -> int:
            return self.now

        def advance_ms(self, milliseconds: int) -> None:
            self.now += milliseconds * 1_000_000

    clock = Clock()
    telemetry = ExecutionTelemetry(clock_ns=clock)
    config = _config()
    config.layout_ir_cache = SimpleNamespace(status="hit")

    clock.advance_ms(5)
    parsing = telemetry.observe(
        {
            "type": "progress_start",
            "stage": "Parse PDF and Create Intermediate Representation",
        },
        config,
    )
    clock.advance_ms(7)
    telemetry.observe(
        {
            "type": "progress_start",
            "stage": "Translate Paragraphs",
        },
        config,
    )
    clock.advance_ms(11)
    telemetry.transition("parsing")
    clock.advance_ms(3)
    telemetry.transition("finalizing")
    clock.advance_ms(13)
    completed = telemetry.finish(config)

    assert parsing["performance"]["phase"] == "parsing"
    assert completed == {
        "schema_version": 1,
        "phase": "completed",
        "elapsed_milliseconds": 39,
        "phase_timings_milliseconds": {
            "launching": 5,
            "parsing": 10,
            "translating": 11,
            "typesetting": 0,
            "saving": 0,
            "finalizing": 13,
        },
        "layout_ir_cache_status": "hit",
    }


@pytest.mark.parametrize("invalid_value", [1, "true", [], None])
def test_layout_ir_cache_enabled_must_be_boolean(
    invalid_value: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    (workroot / "input.pdf").write_bytes(b"fixture")
    monkeypatch.setenv(WORKROOT_ENV, str(workroot))
    request = _complete_execution_request(secrets.token_urlsafe(32))
    request["assets"]["layout_ir_cache"]["enabled"] = invalid_value

    with pytest.raises(
        ValueError,
        match="assets.layout_ir_cache.enabled must be a boolean",
    ):
        build_translation_config(request)
