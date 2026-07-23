from __future__ import annotations

import pytest
from babeldoc.assets import assets


def test_font_resource_lookup_is_cached_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    assets.get_font_and_metadata.cache_clear()
    monkeypatch.setattr(
        assets,
        "get_font_and_metadata_async",
        lambda font_name: calls.append(font_name) or ("font-path", {"name": font_name}),
    )
    monkeypatch.setattr(assets, "run_coro", lambda result: result)

    try:
        first = assets.get_font_and_metadata("fallback.ttf")
        second = assets.get_font_and_metadata("fallback.ttf")
    finally:
        assets.get_font_and_metadata.cache_clear()

    assert first == second == ("font-path", {"name": "fallback.ttf"})
    assert calls == ["fallback.ttf"]
