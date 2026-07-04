"""Observed-QPE antecedent provider: hermetic aggregation/precedence + a live smoke test.

Covers the watershed-aggregated observed-QPE antecedent proxy (§16.1 modifier, FR-3): the
currency gate, the basin mean -> threshold classification, that observed supersedes the
Open-Meteo model point value, and graceful degradation (NFR-6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import box

from upstreamwx.config import Settings
from upstreamwx.engine.models import ActivityType, Mission
from upstreamwx.ingest import qpe_provider
from upstreamwx.ingest.base import IngestBundle

# A small synthetic basin domain (NW Georgia box) and a polygon inside it.
_POLY = box(-85.56, 34.56, -85.44, 34.70)
_NOW = datetime(2026, 7, 4, 13, 0, tzinfo=UTC)
_FNAME = "MRMS_MultiSensor_QPE_72H_Pass2_00.00_20260704-120000.grib2.gz"
_VALID = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _grid(value_in: float) -> xr.DataArray:
    """A uniform QPE grid (inches) over the test domain, MRMS-style 1D lat (desc)/lon."""
    lats = np.arange(34.80, 34.49, -0.05)
    lons = np.arange(-85.60, -85.29, 0.05)
    vals = np.full((lats.size, lons.size), value_in, dtype="float32")
    return xr.DataArray(
        vals, coords={"latitude": lats, "longitude": lons},
        dims=["latitude", "longitude"], name="qpe",
    )


def _mission(start: datetime) -> Mission:
    naive = start.replace(tzinfo=None)
    return Mission(
        activity_type=ActivityType.CANYON, lat=34.6287, lon=-85.4555,
        window_start=naive, window_end=naive + timedelta(hours=6), name="verify",
    )


def _patch_source(monkeypatch, tmp_path, value_in: float):
    """Stub file discovery + decode so aggregation runs on a synthetic grid (no network)."""
    monkeypatch.setattr(qpe_provider, "list_files", lambda **k: [(_VALID, _FNAME)])
    monkeypatch.setattr(qpe_provider, "_select_file", lambda *a, **k: (_VALID, _FNAME))
    monkeypatch.setattr(qpe_provider, "decode_cached", lambda *a, **k: _grid(value_in))
    # Pre-create the decompressed dest so the download branch is skipped.
    dest = tmp_path / "mrms" / _FNAME[:-3]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"stub")


# --- currency gate -----------------------------------------------------------------


def test_select_file_picks_newest_within_age():
    older = (datetime(2026, 7, 4, 6, tzinfo=UTC), "a.gz")
    newer = (datetime(2026, 7, 4, 12, tzinfo=UTC), "b.gz")
    target = datetime(2026, 7, 4, 13, tzinfo=UTC)
    assert qpe_provider._select_file([older, newer], target, max_age_h=12.0) == newer


def test_select_file_none_when_stale():
    old = (datetime(2026, 7, 1, 0, tzinfo=UTC), "a.gz")
    target = datetime(2026, 7, 4, 13, tzinfo=UTC)
    assert qpe_provider._select_file([old], target, max_age_h=12.0) is None


def test_future_mission_declines_observed(monkeypatch, tmp_path):
    # A mission starting well beyond the observed horizon keeps the model proxy (returns None).
    _patch_source(monkeypatch, tmp_path, 1.0)
    settings = Settings(data_dir=tmp_path, mrms_future_window_h=24.0)
    far = _mission(_NOW + timedelta(hours=72))
    res = qpe_provider.antecedent_over_polygon(
        far, _POLY, now=_NOW, settings=settings, data_dir=tmp_path
    )
    assert res is None


# --- aggregation + classification --------------------------------------------------


def test_wet_basin_flags_true(monkeypatch, tmp_path):
    _patch_source(monkeypatch, tmp_path, 0.40)  # > 0.25" threshold
    bundle = IngestBundle()
    qpe_provider.fetch(_mission(_NOW), bundle, _POLY, now=_NOW)
    assert bundle.antecedent_precip_24_72h is True
    assert bundle.antecedent_source == "mrms_qpe_72h"
    assert bundle.antecedent_qpe_mean_in == pytest.approx(0.40, abs=1e-3)
    assert bundle.sources_ok[qpe_provider.NAME] is True


def test_dry_basin_flags_false(monkeypatch, tmp_path):
    _patch_source(monkeypatch, tmp_path, 0.10)  # < 0.25" threshold
    bundle = IngestBundle()
    qpe_provider.fetch(_mission(_NOW), bundle, _POLY, now=_NOW)
    assert bundle.antecedent_precip_24_72h is False
    assert bundle.antecedent_source == "mrms_qpe_72h"


# --- precedence + degradation ------------------------------------------------------


def test_observed_qpe_supersedes_model_point(monkeypatch, tmp_path):
    # Open-Meteo point says dry (False); observed basin QPE says wet -> observed wins.
    from upstreamwx.ingest import gefs_provider, nws, openmeteo, refs_provider, spc

    _patch_source(monkeypatch, tmp_path, 0.40)
    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))

    def om_fetch(m, b):
        b.antecedent_precip_24_72h = False  # model point missed the localized rain
        b.sources_ok["open_meteo"] = True

    monkeypatch.setattr(openmeteo, "fetch", om_fetch)
    monkeypatch.setattr(nws, "fetch", lambda m, b: None)
    monkeypatch.setattr(spc, "fetch", lambda m, b: None)
    monkeypatch.setattr(gefs_provider, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(refs_provider, "fetch", lambda *a, **k: None)

    from upstreamwx.ingest import gather

    # Start ~ now so the future-window gate passes; _select_file is stubbed, so this is
    # independent of the wall clock relative to the fixed synthetic file time.
    bundle = gather(_mission(datetime.now(UTC)), polygon=_POLY)
    assert bundle.antecedent_precip_24_72h is True
    assert bundle.antecedent_source == "mrms_qpe_72h"


def test_run_qpe_degrades_gracefully(monkeypatch, tmp_path):
    from upstreamwx.ingest import orchestrator

    def boom(*a, **k):
        raise RuntimeError("mrms down")

    monkeypatch.setattr(qpe_provider, "fetch", boom)
    b = orchestrator._run_qpe(_mission(_NOW), _POLY)
    assert b.sources_ok.get(qpe_provider.NAME) is False
    assert b.antecedent_precip_24_72h is None  # untouched -> model point still stands
    assert any("MRMS QPE unavailable" in n for n in b.notes)


def test_run_qpe_no_polygon_is_noop():
    from upstreamwx.ingest import orchestrator

    b = orchestrator._run_qpe(_mission(_NOW), None)
    assert b.antecedent_source is None
    assert b.antecedent_precip_24_72h is None


# --- live smoke test (opt-in) ------------------------------------------------------


@pytest.mark.network
def test_qpe_live(tmp_path):
    """Live MRMS reachability + basin aggregation for a real point (NW Georgia)."""
    from upstreamwx.watershed import delineate_cached

    settings = Settings(data_dir=tmp_path)
    basin = delineate_cached(34.6287, -85.4555, settings=settings)
    res = qpe_provider.antecedent_over_polygon(
        _mission(datetime.now(UTC)), basin.polygon, settings=settings, data_dir=tmp_path
    )
    if res is None:
        pytest.skip("no sufficiently-current MRMS file (feed lag)")
    assert res.mean_in is None or res.mean_in >= 0.0
