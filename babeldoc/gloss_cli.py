# Copyright (C) 2026 SamsonCJ and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightweight process boundary between Gloss and the BabelDOC runtime."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from importlib import metadata
from typing import Any

from babeldoc import __version__ as source_version

RUNTIME_NAME = "gloss-babeldoc"
RUNTIME_API_VERSION = 1
SCHEMA_VERSION = 1
UPSTREAM_NAME = "BabelDOC"
UPSTREAM_REPOSITORY = "https://github.com/funstory-ai/BabelDOC"
UPSTREAM_VERSION = "0.6.4"
UPSTREAM_COMMIT = "17480db9df92ddcb37349ce34b312335226e8ec9"
CAPABILITIES = (
    "executor.events.ndjson.v1",
    "executor.http.v1",
    "runtime-info.v1",
)


def package_version() -> str:
    """Return installed distribution metadata, with a source-tree fallback."""
    try:
        return metadata.version("BabelDOC")
    except metadata.PackageNotFoundError:
        return source_version


def build_runtime_info() -> dict[str, Any]:
    """Build the stable, side-effect-free runtime capability payload."""
    return {
        "schema_version": SCHEMA_VERSION,
        "runtime_api_version": RUNTIME_API_VERSION,
        "runtime": {
            "name": RUNTIME_NAME,
            "version": package_version(),
        },
        "upstream": {
            "name": UPSTREAM_NAME,
            "repository": UPSTREAM_REPOSITORY,
            "version": UPSTREAM_VERSION,
            "commit": UPSTREAM_COMMIT,
        },
        "capabilities": sorted(CAPABILITIES),
    }


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=RUNTIME_NAME,
        description="Gloss integration commands for the BabelDOC runtime.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    runtime_info = commands.add_parser(
        "runtime-info",
        help="Report runtime identity and supported protocol capabilities.",
    )
    runtime_info.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON object.",
    )
    serve = commands.add_parser(
        "serve",
        help="Run the authenticated loopback PDF executor service.",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument(
        "--runner",
        choices=("babeldoc", "fake"),
        default="babeldoc",
    )
    serve.add_argument("--token-file")
    serve.add_argument("--work-dir")
    serve.add_argument("--instance-id")
    serve.add_argument("--parent-pid", type=int)
    serve.add_argument("--parent-start-time", type=float)
    return parser


def cli(argv: Sequence[str] | None = None) -> int:
    """Run the lightweight Gloss integration CLI."""
    args = create_parser().parse_args(argv)
    if args.command == "serve":
        from babeldoc.tools.executor.server import serve

        serve(
            args.host,
            args.port,
            runner_name=args.runner,
            token_file=args.token_file,
            work_dir=args.work_dir,
            instance_id=args.instance_id,
            parent_pid=args.parent_pid,
            parent_start_time=args.parent_start_time,
        )
        return 0
    if args.command != "runtime-info":  # pragma: no cover - argparse owns routing
        raise AssertionError(f"Unexpected command: {args.command}")

    payload = build_runtime_info()
    if args.json:
        print(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        )
    else:
        runtime = payload["runtime"]
        upstream = payload["upstream"]
        print(
            f"{runtime['name']} {runtime['version']} "
            f"(upstream {upstream['name']} {upstream['version']}, "
            f"runtime API {payload['runtime_api_version']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
