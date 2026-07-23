from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import select
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from babeldoc import __version__
from babeldoc.tools.executor.layout_server import READY_PREFIX as LAYOUT_READY_PREFIX
from babeldoc.tools.executor.server import READY_PREFIX as EXECUTOR_READY_PREFIX


def smoke(executable: Path) -> None:
    runtime_info = subprocess.run(  # noqa: S603
        [executable, "runtime-info", "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(runtime_info.stdout)
    if payload["runtime"]["version"] != __version__:
        raise RuntimeError("packaged runtime version does not match")
    bundle_root = executable.parent
    for name in ("LICENSE", "NOTICE", "README.txt", "RUNTIME_INFO.json"):
        packaged_file = bundle_root / name
        if not packaged_file.is_file() or packaged_file.stat().st_size == 0:
            raise RuntimeError(f"packaged runtime is missing {name}")
    if json.loads((bundle_root / "RUNTIME_INFO.json").read_bytes()) != payload:
        raise RuntimeError("packaged runtime identity record does not match")
    readme = (bundle_root / "README.txt").read_text(encoding="utf-8")
    if payload["runtime"]["version"] not in readme or "Exact source:" not in readme:
        raise RuntimeError("packaged runtime README lacks provenance")
    dependency_check = subprocess.run(  # noqa: S603
        [executable, "package-smoke"],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if json.loads(dependency_check.stdout) != {
        "ok": True,
        "schema_version": 1,
    }:
        raise RuntimeError("packaged native dependency check failed")
    _smoke_layout(executable)
    _smoke_executor(executable, runner_name="fake")
    _smoke_executor(executable, runner_name="babeldoc")


def _smoke_layout(executable: Path) -> None:
    environment = os.environ.copy()
    environment["BABELDOC_LAYOUT_ALLOW_FAKE"] = "1"
    process = subprocess.Popen(  # noqa: S603
        [
            executable,
            "layout-serve",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--parent-pid",
            str(os.getpid()),
            "--model",
            "fake",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        port = int(_ready_line(process, LAYOUT_READY_PREFIX))
        status, body = _json_request(f"http://127.0.0.1:{port}", "GET", "/healthz")
        if status != 200 or body.get("status") != "ok":
            raise RuntimeError("packaged layout service health check failed")
    finally:
        process.terminate()
        process.wait(timeout=10)


def _smoke_executor(executable: Path, *, runner_name: str) -> None:
    with tempfile.TemporaryDirectory(prefix="gloss-runtime-smoke-") as temporary:
        workroot = Path(temporary)
        workroot.chmod(0o700)
        marker = workroot / ".executor-workroot-ready"
        marker.write_text("ready\n", encoding="utf-8")
        marker.chmod(0o600)
        token = secrets.token_urlsafe(32)
        token_file = workroot / "token"
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        environment = os.environ.copy()
        if runner_name == "fake":
            environment["BABELDOC_EXECUTOR_ALLOW_FAKE"] = "1"
        else:
            environment.pop("BABELDOC_EXECUTOR_ALLOW_FAKE", None)
        process = subprocess.Popen(  # noqa: S603
            [
                executable,
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--runner",
                runner_name,
                "--token-file",
                token_file,
                "--work-dir",
                workroot,
                "--parent-pid",
                str(os.getpid()),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready = json.loads(_ready_line(process, EXECUTOR_READY_PREFIX))
            headers = {"Authorization": f"Bearer {token}"}
            status, body = _json_request(
                ready["endpoint"],
                "GET",
                "/healthz",
                headers=headers,
            )
            if status != 200 or body.get("ok") is not True:
                raise RuntimeError("packaged executor health check failed")
            status, _body = _json_request(
                ready["endpoint"],
                "POST",
                "/v1/shutdown",
                headers={**headers, "Content-Type": "application/json"},
                body=b"{}",
            )
            if status != 202:
                raise RuntimeError("packaged executor shutdown failed")
            if process.wait(timeout=10) != 0:
                raise RuntimeError("packaged executor exited unsuccessfully")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def _ready_line(process: subprocess.Popen, prefix: str) -> str:
    if process.stdout is None:
        raise RuntimeError("service stdout is unavailable")
    readable, _, _ = select.select([process.stdout], [], [], 120)
    if not readable:
        raise TimeoutError("service did not report ready")
    line = process.stdout.readline().strip()
    if not line.startswith(prefix):
        error = process.stderr.read() if process.stderr is not None else ""
        raise RuntimeError(
            f"invalid service ready line: {line}; stderr={error[-2000:]}"
        )
    return line.removeprefix(prefix)


def _json_request(
    endpoint: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict]:
    parsed = urlparse(endpoint)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    connection.request(method, path, headers=headers or {}, body=body)
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    return response.status, payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    smoke(args.executable.resolve(strict=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
