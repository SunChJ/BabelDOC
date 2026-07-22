from __future__ import annotations

import os
from pathlib import Path

WORKROOT_ENV = "BABELDOC_EXECUTOR_WORKROOT"
WORKROOT_READY_FILE = ".executor-workroot-ready"


def get_workroot(*, require_ready_file: bool = False) -> Path:
    raw = os.environ.get(WORKROOT_ENV)
    if not raw:
        raise ValueError(f"{WORKROOT_ENV} is required")
    if "\x00" in raw:
        raise ValueError(f"{WORKROOT_ENV} contains a NUL byte")
    workroot = Path(os.path.normpath(os.path.realpath(raw)))
    if not workroot.is_dir():
        raise ValueError("executor workroot must be an existing directory")
    if not os.access(workroot, os.R_OK | os.W_OK):
        raise ValueError("executor workroot must be readable and writable")
    if require_ready_file and not (workroot / WORKROOT_READY_FILE).is_file():
        raise ValueError("executor workroot readiness proof file is missing")
    return workroot


def resolve_inside_workroot(workroot: Path, value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty string")
    if "\x00" in value:
        raise ValueError("path contains a NUL byte")

    safe_root = os.path.normcase(
        os.path.normpath(os.path.realpath(os.fspath(workroot))),
    )
    candidate = value
    if not os.path.isabs(candidate):  # noqa: PTH117 - CodeQL path sanitizer
        candidate = os.path.join(  # noqa: PTH118 - CodeQL path sanitizer
            safe_root,
            candidate,
        )
    resolved = os.path.normcase(os.path.normpath(os.path.realpath(candidate)))

    # Keep the normalized value that reaches filesystem APIs behind a direct
    # prefix guard. CodeQL recognizes this shape as a safe path access check.
    if not resolved.startswith(safe_root):
        raise ValueError("path escapes executor workroot")

    # The separator-bounded check prevents prefix confusion such as allowing
    # /work/jobs-attacker when the trusted root is /work/jobs.
    root_prefix = safe_root
    if not root_prefix.endswith(os.sep):
        root_prefix += os.sep
    if resolved != safe_root and not resolved.startswith(root_prefix):
        raise ValueError("path escapes executor workroot")
    return Path(resolved)


def resolve_file(workroot: Path, value: str) -> Path:
    path = resolve_inside_workroot(workroot, value)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path


def resolve_dir(workroot: Path, value: str, *, create: bool = False) -> Path:
    path = resolve_inside_workroot(workroot, value)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"{value} must resolve to a directory")
    return path


def relative_to_workroot(workroot: Path, value: Path | str | None) -> str | None:
    if value is None:
        return None
    raw = os.fspath(value)
    if not raw:
        raise ValueError("path must be a non-empty string")
    if "\x00" in raw:
        raise ValueError("path contains a NUL byte")
    raw = os.path.realpath(raw)
    resolved = resolve_inside_workroot(workroot, raw)
    safe_root = os.path.normpath(os.path.realpath(os.fspath(workroot)))
    return os.path.relpath(resolved, safe_root)
