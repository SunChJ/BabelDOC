from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import UTC
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import tomllib
from babeldoc.gloss_cli import CAPABILITIES
from babeldoc.gloss_cli import UPSTREAM_COMMIT
from babeldoc.gloss_cli import UPSTREAM_REPOSITORY
from babeldoc.gloss_cli import UPSTREAM_VERSION

PINNED_PUBLIC_KEY_BASE64 = "0lgbX+CkmBjf4BnH9JO66I7Krd1DYM8lTOjIt+7zWEE="
RUNTIME_ARCHITECTURES = ("arm64", "x86_64")


def build_release_metadata(
    release_dir: Path,
    *,
    repository: str,
    commit: str,
    source_date_epoch: int,
) -> tuple[dict, dict]:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    release_tag = f"v{version.replace('+', '-')}"
    release_base_url = (
        f"https://github.com/{repository}/releases/download/{release_tag}"
    )
    published_at = (
        datetime.fromtimestamp(source_date_epoch, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    runtime_assets = []
    for architecture in RUNTIME_ARCHITECTURES:
        filename = (
            f"gloss-babeldoc-{version.replace('+', '-')}-macos-{architecture}.tar.gz"
        )
        path = _required_file(release_dir / filename)
        runtime_assets.append(
            {
                "operatingSystem": "macos",
                "architecture": architecture,
                "url": f"{release_base_url}/{quote(filename)}",
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "archiveFormat": "tar.gz",
                "executablePath": "gloss-babeldoc-runtime/gloss-babeldoc",
            }
        )

    notes_name = f"v{version.replace('+', '-')}.md"
    signing_key = base64.b64decode(PINNED_PUBLIC_KEY_BASE64, validate=True)
    manifest = {
        "schemaVersion": 1,
        "channel": "stable",
        "version": version,
        "releaseTag": release_tag,
        "publishedAt": published_at,
        "minimumGlossVersion": "0.8.0",
        "releaseNotesURL": (
            f"https://github.com/{repository}/blob/{release_tag}/"
            f"docs/release-notes/{notes_name}"
        ),
        "assets": runtime_assets,
        "capabilities": sorted(CAPABILITIES),
        "upstream": {
            "repository": UPSTREAM_REPOSITORY,
            "version": UPSTREAM_VERSION,
            "commit": UPSTREAM_COMMIT,
        },
        "signature": {
            "algorithm": "Ed25519",
            "encoding": "raw",
            "asset": "gloss-runtime-manifest.json.sig",
            "publicKeySha256": hashlib.sha256(signing_key).hexdigest(),
        },
        "source": {
            "repository": f"https://github.com/{repository}",
            "commit": commit,
            "tag": release_tag,
        },
    }

    subjects = []
    for path in sorted(release_dir.iterdir(), key=lambda item: item.name):
        if path.is_file() and path.name not in {
            "gloss-runtime-manifest.json",
            "gloss-runtime-manifest.json.sig",
            "gloss-runtime-manifest.sig",
            "SHA256SUMS",
            "PROVENANCE.json",
        }:
            subjects.append(
                {
                    "name": path.name,
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
            )
    provenance = {
        "schemaVersion": 1,
        "builder": "github-actions",
        "source": {
            "repository": f"https://github.com/{repository}",
            "commit": commit,
            "tag": release_tag,
            "sourceDateEpoch": source_date_epoch,
        },
        "upstream": manifest["upstream"],
        "dependencyLock": {
            "name": "uv.lock",
            "sha256": _sha256(Path("uv.lock")),
        },
        "subjects": subjects,
    }
    return manifest, provenance


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _required_file(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size <= 0:
        raise FileNotFoundError(f"required release artifact is missing: {path}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    args = parser.parse_args()
    manifest, provenance = build_release_metadata(
        args.release_dir,
        repository=args.repository,
        commit=args.commit,
        source_date_epoch=args.source_date_epoch,
    )
    write_json(args.release_dir / "gloss-runtime-manifest.json", manifest)
    write_json(args.release_dir / "PROVENANCE.json", provenance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
