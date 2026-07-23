from __future__ import annotations

from pathlib import Path

from babeldoc.pdfminer.converter import XMLConverter


def test_codeql_pdf_crypto_suppressions_are_line_scoped() -> None:
    source = (
        Path(__file__).parents[1] / "babeldoc" / "pdfminer" / "pdfdocument.py"
    ).read_text(encoding="utf-8")
    lines = source.splitlines()
    codeql_marker = "# codeql[py/weak-sensitive-data-hashing]"
    codeql_marker_indexes = [
        index for index, line in enumerate(lines) if line.strip() == codeql_marker
    ]
    assert len(codeql_marker_indexes) == 3
    assert all(
        any(call in lines[index + 1] for call in ("sha256(", "next_hash("))
        for index in codeql_marker_indexes
    )
    assert "partial(md5, usedforsecurity=False)" in source
    assert source.count("_pdf_spec_md5(") == 7


def test_xml_control_filter_removes_only_disallowed_xml_controls() -> None:
    allowed = "\t\n\r visible"
    disallowed = "".join(chr(value) for value in [0, 1, 8, 11, 12, 14, 31])

    assert XMLConverter.CONTROL.sub("", disallowed + allowed) == allowed
