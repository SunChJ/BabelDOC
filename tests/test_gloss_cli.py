# Copyright (C) 2026 SamsonCJ and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest
from babeldoc.gloss_cli import build_runtime_info
from babeldoc.gloss_cli import cli


def test_build_runtime_info_contract() -> None:
    payload = build_runtime_info()

    assert set(payload) == {
        "capabilities",
        "runtime",
        "runtime_api_version",
        "schema_version",
        "upstream",
    }
    assert payload["schema_version"] == 1
    assert payload["runtime_api_version"] == 1
    assert payload["runtime"] == {
        "name": "gloss-babeldoc",
        "version": metadata.version("BabelDOC"),
    }
    assert payload["upstream"] == {
        "name": "BabelDOC",
        "repository": "https://github.com/funstory-ai/BabelDOC",
        "version": "0.6.4",
        "commit": "17480db9df92ddcb37349ce34b312335226e8ec9",
    }
    assert payload["capabilities"] == ["runtime-info.v1"]


def test_runtime_info_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["runtime-info", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n")
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out) == build_runtime_info()


def test_runtime_info_is_lightweight_and_side_effect_free(tmp_path: Path) -> None:
    probe = """
import json
import sys
from pathlib import Path

from babeldoc.gloss_cli import cli

exit_code = cli(["runtime-info", "--json"])
heavy_roots = (
    "babeldoc.main",
    "babeldoc.const",
    "babeldoc.format.pdf.high_level",
    "configargparse",
    "fitz",
    "openai",
    "onnxruntime",
)
loaded = sorted(
    module
    for module in sys.modules
    if any(module == root or module.startswith(f"{root}.") for root in heavy_roots)
)
print(json.dumps({"exit_code": exit_code, "loaded": loaded}))
"""
    home = tmp_path / "home"
    home.mkdir()
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
    )

    lines = completed.stdout.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == build_runtime_info()
    assert json.loads(lines[1]) == {"exit_code": 0, "loaded": []}
    assert completed.stderr == ""
    assert not (home / ".cache" / "babeldoc").exists()


def test_unknown_command_exits_with_argparse_error() -> None:
    with pytest.raises(SystemExit) as error:
        cli(["unknown"])

    assert error.value.code == 2
