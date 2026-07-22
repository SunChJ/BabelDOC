from __future__ import annotations

from pathlib import Path

import pytest
from babeldoc.tools.executor import workroot as workroot_module
from babeldoc.tools.executor.workroot import WORKROOT_ENV
from babeldoc.tools.executor.workroot import get_workroot
from babeldoc.tools.executor.workroot import relative_to_workroot
from babeldoc.tools.executor.workroot import resolve_dir
from babeldoc.tools.executor.workroot import resolve_file
from babeldoc.tools.executor.workroot import resolve_inside_workroot


def test_get_workroot_returns_the_real_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    alias = tmp_path / "workroot-alias"
    alias.symlink_to(workroot, target_is_directory=True)
    monkeypatch.setenv(WORKROOT_ENV, str(alias))

    assert get_workroot() == workroot


def test_get_workroot_rejects_empty_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(WORKROOT_ENV, "")

    with pytest.raises(ValueError):
        get_workroot()


def test_get_workroot_rejects_nul_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workroot_module.os,
        "environ",
        {WORKROOT_ENV: "bad\x00path"},
    )

    with pytest.raises(ValueError, match="NUL byte"):
        get_workroot()


def test_resolve_inside_workroot_accepts_contained_relative_and_absolute_paths(
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    nested = workroot / "nested"
    nested.mkdir(parents=True)
    target = nested / "document.pdf"

    assert resolve_inside_workroot(workroot, ".") == workroot
    assert resolve_inside_workroot(workroot, "nested/document.pdf") == target
    assert resolve_inside_workroot(workroot, str(target)) == target
    assert (
        resolve_inside_workroot(
            workroot,
            "nested/../nested/document.pdf",
        )
        == target
    )


@pytest.mark.parametrize("value", ["", "\x00", "nested/file\x00.pdf"])
def test_resolve_inside_workroot_rejects_empty_and_nul_paths(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        resolve_inside_workroot(tmp_path, value)


def test_resolve_inside_workroot_rejects_traversal_and_prefix_confusion(
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "jobs"
    workroot.mkdir()
    prefix_sibling = tmp_path / "jobs-attacker"
    prefix_sibling.mkdir()

    rejected = [
        "../secret.pdf",
        "nested/../../../secret.pdf",
        str(tmp_path / "secret.pdf"),
        str(prefix_sibling / "secret.pdf"),
    ]
    for value in rejected:
        with pytest.raises(ValueError, match="escapes executor workroot"):
            resolve_inside_workroot(workroot, value)


def test_resolve_inside_workroot_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    outside = tmp_path / "outside"
    workroot.mkdir()
    outside.mkdir()
    (workroot / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes executor workroot"):
        resolve_inside_workroot(workroot, "escape/document.pdf")


def test_resolve_inside_workroot_allows_symlink_to_contained_directory(
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    nested = workroot / "nested"
    nested.mkdir(parents=True)
    (workroot / "alias").symlink_to(nested, target_is_directory=True)

    assert resolve_inside_workroot(workroot, "alias/document.pdf") == (
        nested / "document.pdf"
    )


def test_resolve_file_and_dir_use_the_safe_root_boundary(tmp_path: Path) -> None:
    workroot = tmp_path / "workroot"
    workroot.mkdir()
    input_file = workroot / "input.pdf"
    input_file.write_bytes(b"pdf")

    assert resolve_file(workroot, "input.pdf") == input_file
    assert resolve_dir(workroot, "output", create=True) == workroot / "output"

    escaped_dir = tmp_path / "escaped"
    with pytest.raises(ValueError, match="escapes executor workroot"):
        resolve_dir(workroot, "../escaped", create=True)
    assert not escaped_dir.exists()


def test_relative_to_workroot_preserves_relative_output_and_rejects_escapes(
    tmp_path: Path,
) -> None:
    workroot = tmp_path / "workroot"
    nested = workroot / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside.pdf"

    assert relative_to_workroot(workroot, nested / "result.pdf") == str(
        Path("nested") / "result.pdf",
    )
    assert relative_to_workroot(workroot, workroot) == "."
    assert relative_to_workroot(workroot, None) is None

    for value in [outside, "", "bad\x00path"]:
        with pytest.raises(ValueError):
            relative_to_workroot(workroot, value)


def test_relative_to_workroot_rejects_symlink_escape(tmp_path: Path) -> None:
    workroot = tmp_path / "workroot"
    outside = tmp_path / "outside"
    workroot.mkdir()
    outside.mkdir()
    escaped_file = outside / "result.pdf"
    escaped_file.touch()
    link = workroot / "linked-result.pdf"
    link.symlink_to(escaped_file)

    with pytest.raises(ValueError, match="escapes executor workroot"):
        relative_to_workroot(workroot, link)
