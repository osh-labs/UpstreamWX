"""Phase-scoped feature vectors: local hazards respond to the forecast per phase (FR-9a, FR-14a/b).

Increment 3 of the hourly-hazard-series work. ``to_phase_hazard_inputs`` reduces the *local*
hazards (heat, cold/wet, lightning) over each phase's own forecast hours so the morning approach
and the evening egress are scored against their own conditions, not the whole-window worst case.
Flash flood is deliberately left window-conservative (upstream-watershed routing, §16.1). This
module pins: (1) the per-phase reduction + the flash-flood carve-out, (2) the fallback-to-window
when a phase has no hourly coverage, (3) that ``assess`` with ``phase_inputs`` diverges phases yet
leaves flash flood and the tiled overall max unchanged for heat/cold, and (4) that the default
(``phase_inputs=None``) path is byte-identical to before (NFR-4).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from upstreamwx.engine import (
    ActivityType,
    Hazard,
    Mission,
    Phase,
    Tier,
    assess,
)
from upstreamwx.ingest.base import (
    ForecastHourly,
    IngestBundle,
    to_hazard_inputs,
    to_phase_hazard_inputs,
)

# Mission window 08:00-18:00 (naive, matching the engine test fixtures). Inferred phases:
# approach = 08-09, technical = 09-17, egress = 17-18.
_T0 = datetime(2026, 6, 20, 8)
_N = 11  # hours 08..18 inclusive

# Hourly series engineered so each local hazard peaks at a *different* time:
#   heat  — cool morning, hot midday, warm evening
#   cold  — cold dawn (approach), warm midday
#   lightning (REFS) — quiet approach/egress, a midday storm in the technical span
_HEAT = [72.0, 74.0, 85.0, 90.0, 95.0, 100.0, 98.0, 92.0, 88.0, 82.0, 80.0]
_FEELS = [35.0, 40.0, 55.0, 65.0, 72.0, 75.0, 70.0, 66.0, 60.0, 55.0, 50.0]


def _iso(hour: int) -> str:
    """ISO valid time at clock ``hour`` on the mission day (e.g. 9 -> 09:00)."""
    return _T0.replace(hour=hour).isoformat()


def _mission(activity: ActivityType = ActivityType.CANYON, **kw) -> Mission:
    return Mission(
        activity_type=activity,
        lat=37.0,
        lon=-112.0,
        window_start=_T0,
        window_end=_T0 + timedelta(hours=_N - 1),
        **kw,
    )


def _bundle(**over) -> IngestBundle:
    dts = [_T0 + timedelta(hours=i) for i in range(_N)]
    fh = ForecastHourly(
        hours=[d.strftime("%H%M") for d in dts],
        temp_f=[None] * _N,
        feels_f=list(_FEELS),
        wind_mph=[None] * _N,
        gust_mph=[None] * _N,
        precip_pct=[None] * _N,
        qpf_in=[None] * _N,
        sky=["—"] * _N,
        hours_dt=dts,
    )
    b = IngestBundle(
        forecast_hourly=fh,
        heat_index_hourly=list(_HEAT),
        # Window-max/min scalars the engine would collapse to today.
        heat_index_f=max(_HEAT),
        apparent_temp_f=min(_FEELS),
        gefs_p_precip=65.0,
        measurable_precip=True,
        gefs_p_tstm=None,
        refs_p_lightning=80.0,  # window max
        # REFS lightning: quiet at the edges (5%), a midday storm (80/75%). 3 h buckets (V-3, V].
        refs_lightning_hourly={
            _iso(9): 5.0,    # covers grid 08,09 (approach)
            _iso(12): 80.0,  # covers 10,11,12
            _iso(15): 75.0,  # covers 13,14,15
            _iso(18): 5.0,   # covers 16,17,18 (egress)
        },
    )
    for k, v in over.items():
        setattr(b, k, v)
    return b


def test_local_hazards_sliced_per_phase_flash_flood_untouched() -> None:
    b = _bundle()
    base = to_hazard_inputs(b)
    pin = to_phase_hazard_inputs(b, _mission(), base)
    assert pin is not None

    # Heat: approach sees only the cool morning; technical sees the midday peak.
    assert pin[Phase.APPROACH].heat_index_f == 74.0   # max(72, 74)
    assert pin[Phase.TECHNICAL].heat_index_f == 100.0
    assert pin[Phase.EGRESS].heat_index_f == 82.0     # max(82, 80)

    # Cold/wet: coldest apparent temp per phase (dawn is the cold one).
    assert pin[Phase.APPROACH].apparent_temp_f == 35.0
    assert pin[Phase.TECHNICAL].apparent_temp_f == 40.0
    assert pin[Phase.EGRESS].apparent_temp_f == 50.0

    # Lightning: quiet at the edges, the storm sits in the (sheltered) technical span.
    assert pin[Phase.APPROACH].refs_p_lightning == 5.0
    assert pin[Phase.EGRESS].refs_p_lightning == 5.0
    assert pin[Phase.TECHNICAL].refs_p_lightning == 80.0

    # Flash flood is left at the window value in *every* phase (upstream routing — §16.1).
    for phase in Phase:
        assert pin[phase].gefs_p_precip == base.gefs_p_precip == 65.0
        assert pin[phase].measurable_precip is True


def test_uncovered_phase_falls_back_to_window_value() -> None:
    # Only a midday REFS entry -> approach/egress have no ensemble coverage and must fall back
    # to the window value rather than becoming a new (spurious) data gap.
    b = _bundle(refs_lightning_hourly={_iso(12): 80.0})
    base = to_hazard_inputs(b)
    pin = to_phase_hazard_inputs(b, _mission(), base)
    assert pin is not None
    assert pin[Phase.APPROACH].refs_p_lightning == base.refs_p_lightning == 80.0
    assert pin[Phase.EGRESS].refs_p_lightning == base.refs_p_lightning == 80.0


def test_no_forecast_axis_returns_none() -> None:
    empty = IngestBundle()
    assert to_phase_hazard_inputs(empty, _mission(), to_hazard_inputs(empty)) is None


def test_assess_diverges_phases_but_preserves_flash_flood_and_overall() -> None:
    b = _bundle()
    base = to_hazard_inputs(b)
    pin = to_phase_hazard_inputs(b, _mission(), base)

    sliced = assess(_mission(), base, phase_inputs=pin)
    windowed = assess(_mission(), base)  # today's behavior (one window vector)

    def _phase(res, phase):
        return next(p for p in res.phases if p.phase is phase)

    # Heat posture is time-resolved: the morning approach is cooler than the midday slot.
    appr_heat = _phase(sliced, Phase.APPROACH).postures[Hazard.HEAT].heat_category
    tech_heat = _phase(sliced, Phase.TECHNICAL).postures[Hazard.HEAT].heat_category
    assert appr_heat < tech_heat

    # Flash flood is untouched by slicing — same posture with or without phase_inputs.
    assert (
        sliced.bluf[Hazard.FLASH_FLOOD].tier
        is windowed.bluf[Hazard.FLASH_FLOOD].tier
        is Tier.HIGH
    )

    # Overall max is unchanged for the tiled hazards (heat's peak is captured by a phase).
    assert sliced.bluf[Hazard.HEAT].heat_category == windowed.bluf[Hazard.HEAT].heat_category

    # Lightning drops: its peak is in the technical span the party is sheltered through, so the
    # applicable approach/egress phases now see only the quiet edges (windowed saw the storm).
    assert windowed.bluf[Hazard.LIGHTNING].tier is Tier.EXTREME
    assert sliced.bluf[Hazard.LIGHTNING].tier < windowed.bluf[Hazard.LIGHTNING].tier


def test_phase_inputs_none_is_identical_to_default() -> None:
    b = _bundle()
    base = to_hazard_inputs(b)
    a = assess(_mission(), base)
    c = assess(_mission(), base, phase_inputs=None)
    assert a.overall_tier is c.overall_tier
    assert {h: p.tier for h, p in a.bluf.items()} == {h: p.tier for h, p in c.bluf.items()}
    assert {h: p.heat_category for h, p in a.bluf.items()} == {
        h: p.heat_category for h, p in c.bluf.items()
    }
