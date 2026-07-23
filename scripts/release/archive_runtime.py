from __future__ import annotations

import argparse
import gzip
import os
import tarfile
from pathlib import Path


def create_archive(source: Path, output: Path, *, epoch: int) -> None:
    source = source.resolve(strict=True)
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
                )


def _add_path(
    archive: tarfile.TarFile,
    path: Path,
    archive_name: str,
    *,
    source_root: Path,
    epoch: int,
) -> None:
    if path.is_symlink():
        resolved_path = path.resolve(strict=True)
        try:
            resolved_path.relative_to(source_root)
        except ValueError as exc:
            raise ValueError(f"runtime symlink escapes the bundle: {path}") from exc
        if not resolved_path.is_file():
            raise ValueError(f"runtime symlink must resolve to a regular file: {path}")
        path = resolved_path
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
