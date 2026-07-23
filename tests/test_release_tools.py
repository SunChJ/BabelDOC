from __future__ import annotations

import base64
import hashlib
import json
import tarfile
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts.release import smoke_runtime
from scripts.release.archive_runtime import create_archive
from scripts.release.runtime_manifest import build_release_metadata
from scripts.release.runtime_manifest import write_json
from scripts.release.sign_manifest import sign_manifest
from scripts.release.sign_manifest import verify_manifest

VERSION = "0.6.4+gloss.3"
TAG_VERSION = "0.6.4-gloss.3"
SOURCE_DATE_EPOCH = 1_750_000_000


def test_packaged_runtime_smoke_starts_fake_and_production_runners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "gloss-babeldoc-runtime"
    bundle.mkdir()
    executable = bundle / "gloss-babeldoc"
    executable.write_bytes(b"packaged executable")
    payload = {"runtime": {"version": VERSION}}
    for name in ("LICENSE", "NOTICE"):
        (bundle / name).write_text(f"{name}\n", encoding="utf-8")
    (bundle / "README.txt").write_text(
        f"Version: {VERSION}\nExact source: test\n",
        encoding="utf-8",
    )
    (bundle / "RUNTIME_INFO.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    def fake_run(arguments, **_kwargs):
        if arguments[1] == "runtime-info":
            stdout = json.dumps(payload)
        elif arguments[1] == "package-smoke":
            stdout = '{"ok":true,"schema_version":1}'
        else:  # pragma: no cover - guards the smoke command contract
            raise AssertionError(arguments)
        return type("Completed", (), {"stdout": stdout})()

    layout_calls: list[Path] = []
    runner_calls: list[str] = []
    monkeypatch.setattr(smoke_runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(
        smoke_runtime,
        "_smoke_layout",
        lambda path: layout_calls.append(path),
    )
    monkeypatch.setattr(
        smoke_runtime,
        "_smoke_executor",
        lambda _path, *, runner_name: runner_calls.append(runner_name),
    )

    smoke_runtime.smoke(executable)

    assert layout_calls == [executable]
    assert runner_calls == ["fake", "babeldoc"]


def test_runtime_archive_is_reproducible_and_preserves_executable(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "gloss-babeldoc-runtime"
    bundle.mkdir()
    executable = bundle / "gloss-babeldoc"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (bundle / "payload.txt").write_text("runtime payload\n", encoding="utf-8")
    (bundle / "runtime-link").symlink_to("payload.txt")
    for name in ("LICENSE", "NOTICE", "README.txt", "RUNTIME_INFO.json"):
        (bundle / name).write_text(f"{name}\n", encoding="utf-8")

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    create_archive(bundle, first, epoch=SOURCE_DATE_EPOCH)
    create_archive(bundle, second, epoch=SOURCE_DATE_EPOCH)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "gloss-babeldoc-runtime",
            "gloss-babeldoc-runtime/LICENSE",
            "gloss-babeldoc-runtime/NOTICE",
            "gloss-babeldoc-runtime/README.txt",
            "gloss-babeldoc-runtime/RUNTIME_INFO.json",
            "gloss-babeldoc-runtime/gloss-babeldoc",
            "gloss-babeldoc-runtime/payload.txt",
            "gloss-babeldoc-runtime/runtime-link",
        ]
        packaged_executable = archive.getmember("gloss-babeldoc-runtime/gloss-babeldoc")
        assert packaged_executable.mode & 0o111
        assert archive.getmember("gloss-babeldoc-runtime/runtime-link").isfile()
        assert not any(member.issym() or member.islnk() for member in members)
        assert all(member.mtime == SOURCE_DATE_EPOCH for member in members)
        assert all(member.uid == member.gid == 0 for member in members)


def test_runtime_archive_rejects_symlinks_outside_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "gloss-babeldoc-runtime"
    bundle.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("not part of the runtime\n", encoding="utf-8")
    (bundle / "escape").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes the bundle"):
        create_archive(
            bundle,
            tmp_path / "runtime.tar.gz",
            epoch=SOURCE_DATE_EPOCH,
        )


def test_release_manifest_has_strict_gloss_asset_contract(tmp_path: Path) -> None:
    for architecture in ("arm64", "x86_64"):
        filename = f"gloss-babeldoc-{TAG_VERSION}-macos-{architecture}.tar.gz"
        (tmp_path / filename).write_bytes(f"archive-{architecture}".encode())
    (tmp_path / f"BabelDOC-{VERSION}-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / f"babeldoc-{VERSION}.tar.gz").write_bytes(b"sdist")

    manifest, provenance = build_release_metadata(
        tmp_path,
        repository="SunChJ/BabelDOC",
        commit="a" * 40,
        source_date_epoch=SOURCE_DATE_EPOCH,
    )

    assert {
        "schemaVersion",
        "channel",
        "version",
        "releaseTag",
        "publishedAt",
        "minimumGlossVersion",
        "releaseNotesURL",
        "assets",
    } <= manifest.keys()
    assert manifest["version"] == VERSION
    assert manifest["releaseTag"] == f"v{TAG_VERSION}"
    assert manifest["publishedAt"] == "2025-06-15T15:06:40Z"
    assert manifest["minimumGlossVersion"] == "0.8.0"
    assert manifest["signature"]["asset"] == "gloss-runtime-manifest.json.sig"
    assert len(manifest["assets"]) == 2
    assert {asset["architecture"] for asset in manifest["assets"]} == {
        "arm64",
        "x86_64",
    }
    for asset in manifest["assets"]:
        path = tmp_path / Path(asset["url"]).name
        assert asset == {
            "operatingSystem": "macos",
            "architecture": asset["architecture"],
            "url": (
                "https://github.com/SunChJ/BabelDOC/releases/download/"
                f"v{TAG_VERSION}/{path.name}"
            ),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
            "archiveFormat": "tar.gz",
            "executablePath": "gloss-babeldoc-runtime/gloss-babeldoc",
        }
    assert provenance["source"]["commit"] == "a" * 40
    assert {subject["name"] for subject in provenance["subjects"]} == {
        f"BabelDOC-{VERSION}-py3-none-any.whl",
        f"babeldoc-{VERSION}.tar.gz",
        f"gloss-babeldoc-{TAG_VERSION}-macos-arm64.tar.gz",
        f"gloss-babeldoc-{TAG_VERSION}-macos-x86_64.tar.gz",
    }

    output = tmp_path / "gloss-runtime-manifest.json"
    write_json(output, manifest)
    assert output.read_bytes().endswith(b"\n")
    assert json.loads(output.read_bytes()) == manifest


def test_release_manifest_requires_both_macos_architectures(
    tmp_path: Path,
) -> None:
    (tmp_path / f"gloss-babeldoc-{TAG_VERSION}-macos-arm64.tar.gz").write_bytes(
        b"archive"
    )
    with pytest.raises(FileNotFoundError, match="x86_64"):
        build_release_metadata(
            tmp_path,
            repository="SunChJ/BabelDOC",
            commit="a" * 40,
            source_date_epoch=SOURCE_DATE_EPOCH,
        )


def test_manifest_signature_is_raw_ed25519_and_fails_closed() -> None:
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    encoded_private_key = base64.b64encode(private_pem).decode()
    public_key_base64 = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    manifest = b'{"schemaVersion":1}\n'

    signature = sign_manifest(
        manifest,
        encoded_private_key=encoded_private_key,
        expected_public_key_base64=public_key_base64,
    )

    assert len(signature) == 64
    verify_manifest(
        manifest,
        signature,
        public_key_base64=public_key_base64,
    )
    with pytest.raises(InvalidSignature):
        verify_manifest(
            manifest + b" ",
            signature,
            public_key_base64=public_key_base64,
        )
    with pytest.raises(ValueError, match="required"):
        sign_manifest(
            manifest,
            encoded_private_key="",
            expected_public_key_base64=public_key_base64,
        )
    other_public_key = base64.b64encode(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    with pytest.raises(ValueError, match="pinned public key"):
        sign_manifest(
            manifest,
            encoded_private_key=encoded_private_key,
            expected_public_key_base64=other_public_key,
        )
