"""Issue #147 — an unreadable ensemble cache root must degrade, never 500 (NFR-6).

``Path.is_dir()`` raises ``PermissionError`` (rather than returning False) when the process
cannot stat the path — on the 2026-07-20 staging box a misdirected data dir turned that into
an unhandled 500 on every briefing (``service.get_briefing -> _cycle_token ->
gefs.cached_cycles``). The readers must treat a missing/unreadable cache root exactly like a
cold cache: return no cycles, log a warning, and let the live probe / cold ingest proceed.

Hermetic — the permission failure is simulated by monkeypatching ``Path.is_dir`` (a real
``chmod 000`` is also exercised where the test does not run as root, which bypasses mode bits).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from upstreamwx.api.cycles import cycle_key
from upstreamwx.config import Settings
from upstreamwx.gefs import cache as gefs_cache
from upstreamwx.ingest import refs_selection
from upstreamwx.sref import cache as sref_cache

NOW = datetime(2026, 7, 20, 12, 30, tzinfo=UTC)

READERS = [
    pytest.param(gefs_cache.cached_cycles, "gefs", id="gefs"),
    pytest.param(refs_selection.cached_cycles, "refs", id="refs"),
    pytest.param(sref_cache.cached_cycles, "sref", id="sref"),
]


def _deny_is_dir(monkeypatch: pytest.MonkeyPatch, denied: Path) -> None:
    """Make ``Path.is_dir`` raise PermissionError for exactly ``denied``."""
    real = Path.is_dir

    def fake(self: Path) -> bool:
        if self == denied:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "is_dir", fake)


@pytest.mark.parametrize(("reader", "sub"), READERS)
def test_permission_denied_root_reads_as_empty(monkeypatch, tmp_path, reader, sub) -> None:
    """A cache root the process cannot stat yields [] (cold-cache path), not an exception."""
    settings = Settings(data_dir=tmp_path)
    _deny_is_dir(monkeypatch, tmp_path / sub)
    assert reader(now=NOW, settings=settings) == []


@pytest.mark.parametrize(("reader", "sub"), READERS)
def test_unreadable_root_listing_reads_as_empty(monkeypatch, tmp_path, reader, sub) -> None:
    """is_dir() succeeding but iterdir() failing (mode 000 root) also degrades to []."""
    root = tmp_path / sub
    (root / "20260720_06").mkdir(parents=True)
    if os.geteuid() == 0:
        # Root bypasses mode bits — simulate the listing failure instead.
        real = Path.iterdir

        def fake(self: Path):
            if self == root:
                raise PermissionError(13, "Permission denied", str(self))
            return real(self)

        monkeypatch.setattr(Path, "iterdir", fake)
        assert reader(now=NOW, settings=Settings(data_dir=tmp_path)) == []
    else:
        os.chmod(root, 0)
        try:
            assert reader(now=NOW, settings=Settings(data_dir=tmp_path)) == []
        finally:
            os.chmod(root, 0o755)


def test_cycle_token_falls_back_when_cache_root_unreadable(monkeypatch, tmp_path) -> None:
    """The briefing cycle token survives an unreadable GEFS root: wall-clock fallback, no 500."""
    from upstreamwx.api import service as service_mod

    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    _deny_is_dir(monkeypatch, tmp_path / "gefs")
    # Feed dark too — forces the last-resort wall-clock boundary (NFR-6).
    monkeypatch.setattr(service_mod.gefs, "latest_available_cycle", lambda now=None: None)

    svc = service_mod.BriefingService()
    assert svc._cycle_token(NOW) == cycle_key(NOW)


def test_health_data_dir_ok_signal(tmp_path) -> None:
    """_data_dir_ok is True for a usable root, False when the dir cannot be created/used."""
    from types import SimpleNamespace

    from upstreamwx.api.app import _data_dir_ok

    assert _data_dir_ok(SimpleNamespace(data_dir=tmp_path / "fresh")) is True
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    assert _data_dir_ok(SimpleNamespace(data_dir=blocker / "sub")) is False
