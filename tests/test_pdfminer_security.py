from __future__ import annotations

from babeldoc.pdfminer.converter import XMLConverter


def test_xml_control_filter_removes_only_disallowed_xml_controls() -> None:
    allowed = "\t\n\r visible"
    disallowed = "".join(chr(value) for value in [0, 1, 8, 11, 12, 14, 31])

    assert XMLConverter.CONTROL.sub("", disallowed + allowed) == allowed
