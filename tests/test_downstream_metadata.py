# Copyright (C) 2026 SamsonCJ and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from importlib import metadata
from pathlib import Path

import toml
from babeldoc.gloss_cli import RUNTIME_API_VERSION
from babeldoc.gloss_cli import UPSTREAM_COMMIT
from babeldoc.gloss_cli import UPSTREAM_VERSION


def test_downstream_metadata_stays_in_sync() -> None:
    repository = Path(__file__).resolve().parents[1]
    project = toml.load(repository / "pyproject.toml")
    provenance = toml.load(repository / "UPSTREAM_BASE.toml")

    package_version = metadata.version("BabelDOC")
    assert project["project"]["version"] == package_version
    assert provenance["downstream"]["version"] == package_version
    assert provenance["downstream"]["runtime_api_version"] == RUNTIME_API_VERSION
    assert provenance["upstream"]["version"] == UPSTREAM_VERSION
    assert provenance["upstream"]["commit"] == UPSTREAM_COMMIT
