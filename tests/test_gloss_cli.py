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
    assert payload["capabilities"] == [
        "executor.events.ndjson.v1",
        "executor.http.v1",
        "runtime-info.v1",
    ]


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


def test_serve_forwards_service_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    received: dict[str, object] = {}
    token_file = tmp_path / "token"

    def fake_serve(host: str, port: int, **kwargs: object) -> None:
        received.update({"host": host, "port": port, **kwargs})

    monkeypatch.setattr("babeldoc.tools.executor.server.serve", fake_serve)

    assert (
        cli(
            [
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "49152",
                "--runner",
                "fake",
                "--token-file",
                str(token_file),
                "--work-dir",
                str(tmp_path),
                "--instance-id",
                "instance-1",
                "--parent-pid",
                "42",
                "--parent-start-time",
                "123.5",
            ]
        )
        == 0
    )
    assert received == {
        "host": "127.0.0.1",
        "port": 49152,
        "runner_name": "fake",
        "token_file": str(token_file),
        "work_dir": str(tmp_path),
        "instance_id": "instance-1",
        "parent_pid": 42,
        "parent_start_time": 123.5,
    }


def test_serve_defaults_to_ephemeral_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def fake_serve(host: str, port: int, **kwargs: object) -> None:
        received.update({"host": host, "port": port, **kwargs})

    monkeypatch.setattr("babeldoc.tools.executor.server.serve", fake_serve)

    assert cli(["serve", "--runner", "fake"]) == 0
    assert received["host"] == "127.0.0.1"
    assert received["port"] == 0
