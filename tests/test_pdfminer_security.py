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
    noqa_marker = "# noqa -- mandated PDF MD5 transform"
    noqa_marker_lines = [line for line in lines if noqa_marker in line]

    assert len(codeql_marker_indexes) == 6
    assert all(
        any(call in lines[index + 1] for call in ("md5(", "sha256(", "next_hash("))
        for index in codeql_marker_indexes
    )
    assert len(noqa_marker_lines) == 2
    assert all(
        "md5(" in line or "PASSWORD_PADDING" in line for line in noqa_marker_lines
    )


def test_xml_control_filter_removes_only_disallowed_xml_controls() -> None:
    allowed = "\t\n\r visible"
    disallowed = "".join(chr(value) for value in [0, 1, 8, 11, 12, 14, 31])

    assert XMLConverter.CONTROL.sub("", disallowed + allowed) == allowed
