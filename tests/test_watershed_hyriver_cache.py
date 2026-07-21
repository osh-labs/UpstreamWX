"""HyRiver HTTP-cache placement — must live under data_dir, not the read-only CWD.

async-retriever defaults its aiohttp SQLite cache to ``./cache/aiohttp_cache.sqlite`` relative
to the process CWD and ``mkdir``s it. Under the SA-06 read-only release tree that raised
``[Errno 30] Read-only file system: 'cache'`` and broke every USGS WBD/NLDI trace — silently
degrading the upstream-watershed flash-flood domain. The watershed entry points now pin the
cache under ``Settings.data_dir`` first (``_hyriver.configure_hyriver_cache``). Hermetic — no
network; the WaterData client is stubbed to capture the env at call time.
"""

from __future__ import annotations

import os

import pytest

from upstreamwx import watershed as ws
from upstreamwx.config import Settings
from upstreamwx.watershed import _hyriver
from upstreamwx.watershed._hyriver import configure_hyriver_cache

_ENV = "HYRIVER_CACHE_NAME"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Reset the module's memory + the env var so tests don't contaminate each other."""
    monkeypatch.setattr(_hyriver, "_ours", None)
    monkeypatch.delenv(_ENV, raising=False)
    yield


def test_configure_points_cache_under_data_dir_and_creates_it(tmp_path):
    settings = Settings(data_dir=tmp_path)
    configure_hyriver_cache(settings)
    expected = tmp_path / "hyriver" / "aiohttp_cache.sqlite"
    assert os.environ[_ENV] == str(expected)
    assert expected.parent.is_dir()          # created, writable — not the CWD default


def test_configure_respects_operator_override(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, "/custom/hyriver.sqlite")
    configure_hyriver_cache(Settings(data_dir=tmp_path))
    assert os.environ[_ENV] == "/custom/hyriver.sqlite"     # left untouched
    assert not (tmp_path / "hyriver").exists()


def test_configure_is_idempotent_and_reasserts_our_pin(tmp_path):
    settings = Settings(data_dir=tmp_path)
    configure_hyriver_cache(settings)
    first = os.environ[_ENV]
    configure_hyriver_cache(settings)                       # our own value -> re-set, not skipped
    assert os.environ[_ENV] == first


def test_resolve_huc12_configures_cache_before_touching_waterdata(tmp_path, monkeypatch):
    """resolve_huc12 must pin the cache env BEFORE the first async-retriever-backed call."""
    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    seen: dict[str, str | None] = {}

    class _FakeWaterData:
        def bygeom(self, *a, **k):
            seen["cache"] = os.environ.get(_ENV)   # capture what the trace would use
            raise RuntimeError("stop before network")

    monkeypatch.setattr("upstreamwx.watershed.huc._water_data", lambda layer: _FakeWaterData())
    with pytest.raises(ValueError):                # no layer resolves -> ValueError (expected)
        ws.resolve_huc12(34.665, -85.361667)
    assert seen["cache"] == str(tmp_path / "hyriver" / "aiohttp_cache.sqlite")
