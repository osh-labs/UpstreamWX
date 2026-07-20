"""Per-forecast-hour hazard-series tests (FR-6; the PWA hazard graphs).

The providers retain the per-forecast-hour ensemble/thermal arrays they used to throw away
after collapsing to the window-max scalars, and :func:`build_hazard_series` resamples them
onto the shared mission-clock axis. These are **display-only** series: this module pins the
merge/alignment logic *and* the non-negotiable that populating them never perturbs the engine
inputs (``to_hazard_inputs`` bit-identical with/without the series — FR-13, NFR-4).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from upstreamwx.ingest.base import (
    ForecastHourly,
    IngestBundle,
    build_hazard_series,
    to_hazard_inputs,
)


def _axis(start: datetime, n: int) -> ForecastHourly:
    """A bare hourly forecast whose only populated field is the clock axis (hours + hours_dt)."""
    dts = [start + timedelta(hours=i) for i in range(n)]
    return ForecastHourly(
        hours=[d.strftime("%H%M") for d in dts],
        temp_f=[None] * n,
        feels_f=[70.0 + i for i in range(n)],
        wind_mph=[None] * n,
        gust_mph=[None] * n,
        precip_pct=[float(10 * i) for i in range(n)],
        qpf_in=[None] * n,
        sky=["—"] * n,
        hours_dt=dts,
    )


def _iso(start: datetime, h: int) -> str:
    return (start + timedelta(hours=h)).isoformat()


def test_gefs_step_hold_across_six_hour_bucket() -> None:
    """A GEFS value at f6 step-holds back across its (0, 6] APCP bucket, then a gap."""
    t0 = datetime(2026, 6, 20, 12, 0)
    fh = _axis(t0, 8)  # hours 0..7 from t0
    bundle = IngestBundle(
        forecast_hourly=fh,
        gefs_precip_hourly={_iso(t0, 6): 40.0},  # covers grid hours 1..6 (t0 exclusive)
    )
    hs = build_hazard_series(bundle)
    assert hs is not None
    # (0, 6]: hour at t0 (delta 0) is *excluded*; hours +1..+6 hold 40; +7 is a gap.
    assert hs.ff_ensemble_pct == [None, 40.0, 40.0, 40.0, 40.0, 40.0, 40.0, None]


def test_per_hour_max_beats_refs_in_window_authority() -> None:
    """Per hour the merge takes max(GEFS, REFS) where both cover it — the FR-19 analogue.

    REFS is authoritative for the *scalar* posture, but the display series shows the worst
    case per hour, so a higher GEFS hour is not masked by a lower REFS hour and vice-versa.
    """
    t0 = datetime(2026, 6, 20, 12, 0)
    fh = _axis(t0, 8)
    bundle = IngestBundle(
        forecast_hourly=fh,
        gefs_precip_hourly={_iso(t0, 6): 70.0},  # 6h bucket (0, 6] -> hours 0..6
        refs_precip_hourly={_iso(t0, 3): 20.0, _iso(t0, 6): 90.0},  # 3h buckets
    )
    hs = build_hazard_series(bundle)
    # Buckets are lower-exclusive: hour0 (the bucket's left edge) is covered by neither -> gap;
    # hours 1-3: GEFS 70 beats the lower REFS -> 70; hours 4-6: REFS 90 beats GEFS 70 -> 90;
    # hour7: no forecast hour covers it -> gap.
    assert hs.ff_ensemble_pct == [None, 70.0, 70.0, 70.0, 90.0, 90.0, 90.0, None]


def test_uncovered_hours_are_none_not_zero() -> None:
    """An hour no ensemble forecast hour covers is a gap (None), never a benign 0 (NFR-6)."""
    t0 = datetime(2026, 6, 20, 12, 0)
    fh = _axis(t0, 3)
    bundle = IngestBundle(forecast_hourly=fh)  # no ensemble arrays at all
    hs = build_hazard_series(bundle)
    assert hs.ff_ensemble_pct == [None, None, None]
    assert hs.lightning_ensemble_pct == [None, None, None]
    # Thermal series fall back to the forecast axis (feels_f) and per-hour heat index (absent).
    assert hs.apparent_temp_f == fh.feels_f
    assert hs.heat_index_f == [None, None, None]


def test_thermal_and_precip_series_align_to_axis() -> None:
    t0 = datetime(2026, 6, 20, 12, 0)
    fh = _axis(t0, 3)
    bundle = IngestBundle(forecast_hourly=fh, heat_index_hourly=[88.0, None, 91.0])
    hs = build_hazard_series(bundle)
    assert hs.precip_pct == fh.precip_pct
    assert hs.heat_index_f == [88.0, None, 91.0]
    assert hs.apparent_temp_f == fh.feels_f
    assert len(hs.ff_ensemble_pct) == len(fh.hours)


def test_no_forecast_axis_yields_none() -> None:
    """Offline/degraded (no forecast_hourly, or no hours_dt) -> None, cards show a placeholder."""
    assert build_hazard_series(IngestBundle()) is None
    fh = _axis(datetime(2026, 6, 20, 12, 0), 2)
    assert build_hazard_series(IngestBundle(forecast_hourly=replace(fh, hours_dt=[]))) is None


def test_display_series_never_change_engine_inputs() -> None:
    """Populating the display series must not perturb to_hazard_inputs (FR-13, NFR-4)."""
    t0 = datetime(2026, 6, 20, 12, 0)
    fh = _axis(t0, 4)
    base = IngestBundle(
        gefs_p_precip=55.0,
        gefs_p_tstm=30.0,
        heat_index_f=98.0,
        apparent_temp_f=44.0,
        member_support={"flash_flood": 0.55},
    )
    enriched = IngestBundle(
        gefs_p_precip=55.0,
        gefs_p_tstm=30.0,
        heat_index_f=98.0,
        apparent_temp_f=44.0,
        member_support={"flash_flood": 0.55},
        forecast_hourly=fh,
        gefs_precip_hourly={_iso(t0, 3): 70.0},
        gefs_tstm_hourly={_iso(t0, 3): 12.0},
        refs_precip_hourly={_iso(t0, 3): 90.0},
        refs_lightning_hourly={_iso(t0, 3): 5.0},
        heat_index_hourly=[88.0, 89.0, 90.0, 91.0],
    )
    enriched.hazard_series = build_hazard_series(enriched)
    assert enriched.hazard_series is not None  # series populated
    assert to_hazard_inputs(base) == to_hazard_inputs(enriched)


def test_build_is_deterministic_regardless_of_dict_order() -> None:
    """Merge is order-independent (max), so insertion order can't change output (NFR-4)."""
    t0 = datetime(2026, 6, 20, 12, 0)
    fh = _axis(t0, 4)
    forward = {_iso(t0, 2): 20.0, _iso(t0, 3): 90.0}
    reverse = {_iso(t0, 3): 90.0, _iso(t0, 2): 20.0}
    a = build_hazard_series(IngestBundle(forecast_hourly=fh, refs_precip_hourly=forward))
    b = build_hazard_series(IngestBundle(forecast_hourly=fh, refs_precip_hourly=reverse))
    assert a.ff_ensemble_pct == b.ff_ensemble_pct
