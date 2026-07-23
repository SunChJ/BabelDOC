from __future__ import annotations

import pickle  # noqa: S403 - used to prove the restricted loader rejects it
from pathlib import Path
from types import SimpleNamespace

from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.tools.executor.layout_ir_cache import LayoutIRCache


def _config(input_file: Path):
    return SimpleNamespace(
        input_file=input_file,
        skip_scanned_detection=True,
        debug=False,
        pages=None,
        page_ranges=None,
        only_parse_generate_pdf=False,
        table_model=None,
        enable_graphic_element_process=True,
        lang_in="en",
        lang_out="zh-CN",
        merge_alternating_line_numbers=True,
        ocr_workaround=False,
        primary_font_family=None,
        remove_non_formula_lines=False,
        skip_curve_render=False,
        skip_form_render=False,
        skip_formula_offset_calculation=False,
        shared_context_cross_split_part=SimpleNamespace(
            valid_char_count_total=120,
            total_valid_text_token_count=30,
        ),
    )


def _cache(tmp_path: Path) -> LayoutIRCache:
    root = tmp_path / "cache"
    root.mkdir(mode=0o700)
    return LayoutIRCache(root, max_pages_per_part=50)


def _document() -> il_version_1.Document:
    return il_version_1.Document(
        page=[il_version_1.Page(page_number=0)],
        total_pages=1,
    )


def test_layout_ir_cache_round_trip_restores_document_and_counts(
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-cache-fixture")
    config = _config(input_file)
    cache = _cache(tmp_path)
    mupdf_document = SimpleNamespace(page_count=1)

    assert cache.load(config, mupdf_document) is None
    assert cache.status == "miss"
    cache.store(_document(), config)
    assert cache.status == "stored"

    config.shared_context_cross_split_part.valid_char_count_total = 0
    config.shared_context_cross_split_part.total_valid_text_token_count = 0
    reloaded = LayoutIRCache(cache.root, max_pages_per_part=50)
    document = reloaded.load(config, mupdf_document)

    assert document == _document()
    assert reloaded.status == "hit"
    assert config.shared_context_cross_split_part.valid_char_count_total == 120
    assert config.shared_context_cross_split_part.total_valid_text_token_count == 30


def test_layout_ir_cache_rejects_untrusted_pickle_globals(tmp_path: Path) -> None:
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-cache-fixture")
    config = _config(input_file)
    cache = _cache(tmp_path)
    mupdf_document = SimpleNamespace(page_count=1)
    assert cache.load(config, mupdf_document) is None
    assert cache._cache_path is not None
    cache._cache_path.write_bytes(pickle.dumps(tmp_path / "not-an-il-document"))
    cache._cache_path.chmod(0o600)

    assert cache.load(config, mupdf_document) is None
    assert cache.status == "invalidated"
    assert not cache._cache_path.exists()


def test_layout_ir_cache_detects_input_change_before_store(tmp_path: Path) -> None:
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-before")
    config = _config(input_file)
    cache = _cache(tmp_path)

    assert cache.load(config, SimpleNamespace(page_count=1)) is None
    input_file.write_bytes(b"%PDF-after")
    cache.store(_document(), config)

    assert cache.status == "input_changed"
    assert not list(cache.root.glob("*.pickle"))


def test_layout_ir_cache_requires_private_directory(tmp_path: Path) -> None:
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-cache-fixture")
    config = _config(input_file)
    cache = _cache(tmp_path)
    cache.root.chmod(0o755)

    assert cache.load(config, SimpleNamespace(page_count=1)) is None
    assert cache.status == "unsafe_directory"


def test_layout_ir_cache_rejects_split_or_scanned_detection_runs(
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-cache-fixture")
    config = _config(input_file)
    cache = _cache(tmp_path)

    assert cache.load(config, SimpleNamespace(page_count=51)) is None
    assert cache.status == "ineligible"

    config.skip_scanned_detection = False
    assert cache.load(config, SimpleNamespace(page_count=1)) is None
    assert cache.status == "ineligible"
