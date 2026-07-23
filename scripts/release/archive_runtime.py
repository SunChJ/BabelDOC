from __future__ import annotations

import argparse
import gzip
import os
import tarfile
from pathlib import Path


def create_archive(source: Path, output: Path, *, epoch: int) -> None:
    source = source.resolve(strict=True)
    try:
        output.resolve(strict=False).relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("runtime archive output must be outside the source bundle")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_output,
            mtime=epoch,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
                dereference=False,
            ) as archive:
                _add_path(
                    archive,
                    source,
                    source.name,
                    source_root=source,
                    epoch=epoch,
                    active_directories=frozenset(),
                )


def _add_path(
    archive: tarfile.TarFile,
    path: Path,
    archive_name: str,
    *,
    source_root: Path,
    epoch: int,
    active_directories: frozenset[tuple[int, int]],
) -> None:
    if path.is_symlink():
        try:
            resolved_path = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"runtime symlink cannot be resolved: {path}") from exc
        try:
            resolved_path.relative_to(source_root)
        except ValueError as exc:
            raise ValueError(f"runtime symlink escapes the bundle: {path}") from exc
        if not resolved_path.is_file() and not resolved_path.is_dir():
            raise ValueError(
                f"runtime symlink must resolve to a regular file or directory: {path}"
            )
        path = resolved_path
    if not path.is_file() and not path.is_dir():
        raise ValueError(f"runtime entry must be a regular file or directory: {path}")
    next_active_directories = active_directories
    if path.is_dir():
        directory_stat = path.stat()
        directory_identity = (directory_stat.st_dev, directory_stat.st_ino)
        if directory_identity in active_directories:
            raise ValueError(f"runtime symlink creates a directory cycle: {path}")
        next_active_directories = active_directories | {directory_identity}
    info = archive.gettarinfo(os.fspath(path), arcname=archive_name)
    if info.issym() or info.islnk():
        raise ValueError(f"runtime archive cannot contain links: {archive_name}")
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = epoch
    if info.isfile():
        with path.open("rb") as handle:
            archive.addfile(info, handle)
    else:
        archive.addfile(info)
    if info.isdir():
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _add_path(
                archive,
                child,
                f"{archive_name}/{child.name}",
                source_root=source_root,
                epoch=epoch,
                active_directories=next_active_directories,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    args = parser.parse_args()
    create_archive(
        args.source,
        args.output,
        epoch=args.source_date_epoch,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
