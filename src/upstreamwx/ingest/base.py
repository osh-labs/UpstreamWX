"""Ingest provider contract and the normalized bundle the engine consumes.

The engine never imports a provider directly (FR-13, §12): every source fills an
:class:`IngestBundle`, which :func:`to_hazard_inputs` maps onto the engine's
:class:`~upstreamwx.engine.models.HazardInputs`. This keeps providers swappable
(e.g. Open-Meteo for a future paid feed) without touching engine logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from ..engine.models import HazardInputs, Mission, Phase
from ..engine.phases import infer_phases

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

    from ..watershed import PourpointBasin

# Step-hold buckets for resampling the sparse ensemble forecast-hour values onto the dense
# mission-clock axis (FR-7/7a cadences). A GEFS value at valid time V is the P over the 6 h
# APCP bucket (V-6, V]; a REFS value covers its ~3 h accumulation window (V-3, V]. Display only.
_GEFS_BUCKET_H = 6
_REFS_BUCKET_H = 3


@dataclass
class ForecastHourly:
    """Per-hour display forecast over the mission window (FR-6; M0.4 PWA Forecast view).

    Built by the Open-Meteo adapter from the *same* query that feeds the engine's derived
    fields, then carried on the bundle so the API can serialize the Forecast view's table
    and charts (PRD §6.8). The engine never reads this — it is display data only and never
    changes a posture (FR-20). Empty/absent under graceful degradation (NFR-6) or on the
    offline ``inputs`` path. All arrays are index-aligned with ``hours``.
    """

    hours: list[str]                       # local "HHMM" labels, mission-window order
    temp_f: list[float | None]             # air temperature, deg F
    feels_f: list[float | None]            # apparent temperature, deg F
    wind_mph: list[float | None]
    gust_mph: list[float | None]
    precip_pct: list[float | None]         # precipitation probability, percent
    qpf_in: list[float | None]             # quantitative precip forecast, inch
    sky: list[str]                         # WMO weather-code emoji per hour
    # Naive-UTC valid time of each hour, index-aligned with ``hours``. Display-only plumbing:
    # the shared mission-clock axis onto which the sparse ensemble forecast hours are resampled
    # (see :func:`build_hazard_series`). Never serialized, never an engine input.
    hours_dt: list[datetime] = field(default_factory=list)


@dataclass
class HazardSeries:
    """Per-forecast-hour hazard series over the mission window (FR-6; PWA hazard graphs).

    The hazard-card analogue of :class:`ForecastHourly`: built from the *same* ingest that
    feeds the engine's scalar hazard inputs, then carried on the bundle so the PWA can graph
    probability-over-mission-time on each hazard card. Like ``ForecastHourly`` the engine
    never reads this — it is display data only and never changes a posture (FR-13, FR-20,
    NFR-4). Every list is index-aligned with ``ForecastHourly.hours`` (the shared mission-clock
    axis); a ``None`` entry is a genuine coverage gap (no ensemble/forecast hour covers that
    clock hour) and must never be read as a benign zero (data quality first-class, NFR-6).
    """

    # Merged ensemble series: REFS authoritative in-window, GEFS beyond, per-hour max where
    # both cover an hour (the per-hour analogue of the scalar FR-19 merge).
    ff_ensemble_pct: list[float | None]         # flash-flood P(precip) %, per hour
    lightning_ensemble_pct: list[float | None]  # lightning P(thunder) %, per hour
    # Thermal / surface-precip display series (Open-Meteo), aligned to the same axis.
    precip_pct: list[float | None]              # hourly precipitation probability, %
    heat_index_f: list[float | None]            # NWS heat index (Rothfusz), deg F
    apparent_temp_f: list[float | None]         # apparent temperature, deg F


@dataclass
class IngestBundle:
    """Normalized fields gathered from all sources for one mission/window."""

    # NWS products + AFD (FR-5).
    flash_flood_warning: bool = False
    flash_flood_watch: bool = False
    flood_warning: bool = False
    flood_advisory: bool = False
    flood_watch: bool = False
    thunderstorm_warning: bool = False
    afd_text: str | None = None
    afd_storm_mode: str | None = None          # isolated | scattered | numerous; None if absent
    afd_convective_mention: bool = False        # derived from afd_storm_mode; display only
    afd_flood_mention: bool = False

    # Open-Meteo derived fields (FR-6), already in deg F / mph / inch. The precip booleans
    # are tri-state: None means "unknown" (source down / window not covered) and must never
    # be conflated with a genuinely dry False — data quality is first-class (NFR-6).
    heat_index_f: float | None = None
    apparent_temp_f: float | None = None
    wind_mph: float | None = None
    measurable_precip: bool | None = None
    antecedent_precip_24_72h: bool | None = None

    # Per-hour display forecast over the window (FR-6; M0.4 Forecast view). Display only,
    # not an engine input; None when ingest could not populate it (NFR-6).
    forecast_hourly: ForecastHourly | None = None

    # Per-forecast-hour display series for the PWA hazard graphs, built by
    # :func:`build_hazard_series` at the end of ingest once the ensemble raw arrays and the
    # forecast-hour axis are both present. Display only, never an engine input (FR-13); None on
    # the offline ``inputs`` path or when no forecast axis was gathered.
    hazard_series: HazardSeries | None = None

    # Raw per-forecast-hour ensemble probabilities the providers compute *before* collapsing to
    # the window-max scalars below (display only, never an engine input). Keyed by the valid
    # time's naive-UTC ISO string -> percent. GEFS steps 6-hourly, REFS 3-hourly; they are
    # resampled onto the mission-clock axis by :func:`build_hazard_series`.
    gefs_precip_hourly: dict[str, float] = field(default_factory=dict)
    gefs_tstm_hourly: dict[str, float] = field(default_factory=dict)
    refs_precip_hourly: dict[str, float] = field(default_factory=dict)
    refs_lightning_hourly: dict[str, float] = field(default_factory=dict)
    # Per-hour NWS heat index (deg F), index-aligned with ``forecast_hourly.hours``; the hourly
    # basis of the ``heat_index_f`` window-max scalar (display only). Apparent temp and precip%
    # per hour already survive on ``forecast_hourly`` (feels_f / precip_pct).
    heat_index_hourly: list[float | None] = field(default_factory=list)

    # GEFS ensemble over the upstream domain (FR-7).
    gefs_p_precip: float | None = None
    gefs_p_tstm: float | None = None
    gefs_cycle: str | None = None  # model run actually used, "YYYYMMDD/HHZ"; provenance
    convective_rate_in_per_hr: float | None = None
    cape_jkg: float | None = None
    member_support: dict[str, float] = field(default_factory=dict)

    # REFS same-day high-resolution supplement over the upstream domain (FR-7a).
    # Set only when the mission window is in REFS range (~6-36 h).
    refs_p_precip: float | None = None
    refs_p_lightning: float | None = None
    refs_in_range: bool = False
    refs_cycle: str | None = None  # primary run, "YYYYMMDD/HHZ"; display only
    refs_fhour: str | None = None  # range label, e.g. "f11" or "f06-f30 (+ 00Z f13-f17 ...)"
    # Per contributing run: (run label, min fhour, max fhour), primary first then spin-up
    # backfills. Multi-run when an older run backfills the current run's spin-up; display only.
    refs_runs: list[tuple[str, int, int]] | None = None

    # GEFS<->REFS cross-ensemble agreement (FR-17, §16.5); feeds the confidence
    # qualifier. "consistent" unless an in-range REFS materially diverges from GEFS.
    source_agreement: str = "consistent"

    # SPC convective outlook category.
    spc_category: str | None = None

    # Upstream contributing-watershed domain used for GEFS/REFS aggregation (FR-3),
    # delineated pour-point-exact (NLDI raindrop two-step) with a WBD fallback. Set
    # by the orchestrator unless an explicit polygon override is passed; carries the
    # provenance (method, area, snapped point, flowline) the SITREP header renders.
    upstream: PourpointBasin | None = None
    # False when the upstream trace may be missing area (external inflow at the widest
    # WBD fetch, a failed completeness probe): flash-flood confidence is capped and the
    # gap is named — a possibly-truncated basin must not present as the full watershed.
    watershed_complete: bool = True

    # Radius of Concern (RoC, FR-3): the upstream watershed clipped to a user-set disk
    # around the mission origin. ``aggregation_polygon`` is the polygon the GEFS/REFS
    # zonal aggregation actually ran over (the clipped ``kept`` when a RoC is set, else
    # the full basin). ``roc_disk``/``roc_excluded`` drive the PWA's dashed-ring and
    # hatched-exclusion rendering; ``roc_excluded`` is None when the basin fits the disk.
    aggregation_polygon: BaseGeometry | None = None
    roc_radius_km: float | None = None
    roc_disk: BaseGeometry | None = None
    roc_excluded: BaseGeometry | None = None
    roc_kept_area_km2: float | None = None

    # Lightning Area of Concern (LAoC): the disk around the activity that the lightning
    # ensemble fields (``gefs_p_tstm``/``refs_p_lightning``) aggregate over instead of the
    # upstream watershed (PRD §16.1, §13 principle 4 — lightning is a point/corridor
    # estimate, not basin-routed). ``laoc_disk`` drives the PWA's yellow ring. None unless
    # the mission sets ``lightning_radius_km``; lightning then falls back to the flood domain.
    laoc_radius_km: float | None = None
    laoc_disk: BaseGeometry | None = None
    laoc_area_km2: float | None = None

    # Provenance / graceful-degradation tracking (NFR-6).
    sources_ok: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def bundle_data_gaps(bundle: IngestBundle) -> list[str]:
    """Name the data gaps affecting a gathered bundle (data quality first-class, NFR-6).

    The single source of truth the Markdown render and the structured contract both
    surface, so a hazard evaluated without its primary input is visibly "unassessed"
    everywhere the briefing is shown — never quietly benign. Display-only (FR-13).
    """
    gaps: list[str] = []
    if bundle.gefs_p_precip is None and bundle.refs_p_precip is None:
        gaps.append("flood ensemble signal unavailable over the upstream domain")
    if bundle.gefs_p_tstm is None and bundle.refs_p_lightning is None:
        gaps.append("lightning ensemble signal unavailable over the exposure area")
    if bundle.heat_index_f is None or bundle.apparent_temp_f is None:
        gaps.append("thermal forecast series unavailable")
    if bundle.measurable_precip is None:
        gaps.append("surface precip signal unavailable (unknown, not dry)")
    if bundle.sources_ok.get("nws") is False:
        gaps.append("NWS active-alert check unavailable (products unverified)")
    if bundle.sources_ok.get("nws_afd") is False:
        gaps.append("NWS forecast discussion unavailable")
    if bundle.sources_ok.get("watershed") is False:
        gaps.append("upstream watershed delineation unavailable")
    if not bundle.watershed_complete:
        gaps.append(
            "upstream watershed trace may be incomplete (basin possibly larger than mapped)"
        )
    return gaps


def _cover(grid_dt: datetime, by_valid: dict[str, float], bucket_h: int) -> float | None:
    """Value whose step-hold bucket ``(V - bucket_h, V]`` covers ``grid_dt`` (max if several).

    The sparse ensemble forecast hours are resampled onto the dense mission-clock axis by
    step-holding each value back across its accumulation/step bucket. Taking the max where more
    than one bucket covers an hour (e.g. a REFS spin-up backfill at the same valid time) is
    commutative, so the result is independent of dict insertion order (NFR-4). Display only.
    """
    best: float | None = None
    for iso, pct in by_valid.items():
        v = datetime.fromisoformat(iso)
        if v - timedelta(hours=bucket_h) < grid_dt <= v:
            best = pct if best is None else max(best, pct)
    return best


def _merge_hour(a: float | None, b: float | None) -> float | None:
    """Per-hour ensemble merge: max where both cover the hour, else whichever exists, else None.

    The per-hour analogue of the scalar GEFS/REFS merge (REFS authoritative in-window, GEFS
    beyond, higher value where both — FR-19). ``None`` is a genuine coverage gap, never a 0.
    """
    vals = [x for x in (a, b) if x is not None]
    return max(vals) if vals else None


def build_hazard_series(bundle: IngestBundle) -> HazardSeries | None:
    """Resample the raw ensemble + thermal arrays onto the mission-clock axis (FR-6, NFR-4).

    Produces the two merged ensemble series (flash-flood P(precip), lightning P(thunder)) plus
    the thermal display series, all index-aligned 1:1 with ``forecast_hourly.hours``. Returns
    ``None`` when no forecast axis was gathered (offline/degraded), so the hazard cards fall
    back to a placeholder. Pure and deterministic — display only, never an engine input (FR-13).
    """
    fh = bundle.forecast_hourly
    if fh is None or not fh.hours_dt:
        return None
    n = len(fh.hours)
    ff: list[float | None] = []
    ltng: list[float | None] = []
    for gdt in fh.hours_dt:
        ff.append(
            _merge_hour(
                _cover(gdt, bundle.gefs_precip_hourly, _GEFS_BUCKET_H),
                _cover(gdt, bundle.refs_precip_hourly, _REFS_BUCKET_H),
            )
        )
        ltng.append(
            _merge_hour(
                _cover(gdt, bundle.gefs_tstm_hourly, _GEFS_BUCKET_H),
                _cover(gdt, bundle.refs_lightning_hourly, _REFS_BUCKET_H),
            )
        )
    heat = list(bundle.heat_index_hourly) if bundle.heat_index_hourly else [None] * n
    return HazardSeries(
        ff_ensemble_pct=ff,
        lightning_ensemble_pct=ltng,
        precip_pct=list(fh.precip_pct),
        heat_index_f=heat,
        apparent_temp_f=list(fh.feels_f),
    )


def _utc_naive(dt: datetime) -> datetime:
    """UTC-naive form of a (possibly tz-aware) datetime, to compare with ``hours_dt``."""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _phase_indices(hours_dt: list[datetime], start: datetime, end: datetime) -> list[int]:
    """Indices of the mission-clock hours falling in a phase window (inclusive bounds).

    Phase windows tile the mission window contiguously (:func:`infer_phases`), so a boundary
    hour is counted in both adjacent phases; with a max/min reduction that overlap is harmless
    and keeps each phase conservative.
    """
    lo, hi = _utc_naive(start), _utc_naive(end)
    return [i for i, dt in enumerate(hours_dt) if lo <= dt <= hi]


def _sliced_or_base(
    values: list[float | None], idx: list[int], how: str, base_val: float | None
) -> float | None:
    """Reduce ``values`` over ``idx`` (``max``/``min``), falling back to ``base_val``.

    A phase with no covered hour for this field falls back to the window value rather than
    ``None`` — never a *new* data gap, and conservative (the window value is the worst case).
    """
    vals = [values[i] for i in idx if i < len(values) and values[i] is not None]
    if not vals:
        return base_val
    return max(vals) if how == "max" else min(vals)


def to_phase_hazard_inputs(
    bundle: IngestBundle, mission: Mission, base: HazardInputs
) -> dict[Phase, HazardInputs] | None:
    """Per-phase feature vectors: slice the *local* hazards to each phase's forecast hours.

    Heat, cold/wet and lightning are point/corridor hazards (§16.1, FR-14a/b) whose posture
    should reflect conditions *while that phase is underway* — the morning approach and the
    evening egress are not the midday slot. Each is reduced over the phase's own hours (heat
    and lightning by max, cold/wet by the coldest apparent temp). **Flash flood is deliberately
    left at the window-max value**: it is upstream-watershed-routed, so rain that fell upstream
    earlier arrives in-slot on a travel-time lag — narrowing it to the in-slot hours would
    *understate* it (the conservative non-negotiable). A phase with no hourly coverage for a
    field falls back to the window value. Returns ``None`` when no forecast axis was gathered
    (offline ``inputs`` path / degraded ingest), so :func:`~upstreamwx.engine.assess.assess`
    uses the single window vector unchanged. Deterministic (NFR-4); the engine still only ever
    consumes :class:`HazardInputs` — this keeps the display-only ``hazard_series`` boundary
    (FR-13) intact by constructing real feature vectors here in ingest, not in the engine.
    """
    fh = bundle.forecast_hourly
    if fh is None or not fh.hours_dt:
        return None
    hours_dt = fh.hours_dt
    # Resample the two lightning ensemble sources *separately* onto the mission-clock axis: the
    # evaluator scores GEFS and REFS on their own cut points and takes the higher tier (FR-19).
    gefs_tstm = [_cover(dt, bundle.gefs_tstm_hourly, _GEFS_BUCKET_H) for dt in hours_dt]
    refs_ltng = [_cover(dt, bundle.refs_lightning_hourly, _REFS_BUCKET_H) for dt in hours_dt]
    heat_hourly = bundle.heat_index_hourly or []
    feels_hourly = fh.feels_f

    windows, _ = infer_phases(mission)
    out: dict[Phase, HazardInputs] = {}
    for phase, (start, end) in windows.items():
        idx = _phase_indices(hours_dt, start, end)
        out[phase] = replace(
            base,
            heat_index_f=_sliced_or_base(heat_hourly, idx, "max", base.heat_index_f),
            apparent_temp_f=_sliced_or_base(feels_hourly, idx, "min", base.apparent_temp_f),
            gefs_p_tstm=_sliced_or_base(gefs_tstm, idx, "max", base.gefs_p_tstm),
            refs_p_lightning=_sliced_or_base(refs_ltng, idx, "max", base.refs_p_lightning),
        )
    return out


class Provider(Protocol):
    """A data source that contributes part of an :class:`IngestBundle`."""

    name: str

    def fetch(self, mission: Mission, bundle: IngestBundle) -> None:
        """Populate the provider's fields on ``bundle`` (in place)."""
        ...


def to_hazard_inputs(bundle: IngestBundle, *, dry_party: bool = False) -> HazardInputs:
    """Map a gathered bundle onto the engine's normalized feature vector.

    Availability crosses the boundary too: the tri-state precip booleans pass through
    unchanged, and a failed NWS fetch marks the alert flags as *unchecked* rather than
    letting their ``False`` defaults read as "no active products" (NFR-6).
    """
    return HazardInputs(
        flash_flood_warning=bundle.flash_flood_warning,
        flash_flood_watch=bundle.flash_flood_watch,
        flood_warning=bundle.flood_warning,
        flood_advisory=bundle.flood_advisory,
        flood_watch=bundle.flood_watch,
        thunderstorm_warning=bundle.thunderstorm_warning,
        gefs_p_precip=bundle.gefs_p_precip,
        gefs_p_tstm=bundle.gefs_p_tstm,
        measurable_precip=bundle.measurable_precip,
        convective_rate_in_per_hr=bundle.convective_rate_in_per_hr,
        cape_jkg=bundle.cape_jkg,
        refs_p_precip=bundle.refs_p_precip,
        refs_p_lightning=bundle.refs_p_lightning,
        member_support=dict(bundle.member_support),
        source_agreement=bundle.source_agreement,
        spc_category=bundle.spc_category,
        afd_storm_mode=bundle.afd_storm_mode,
        afd_flood_mention=bundle.afd_flood_mention,
        heat_index_f=bundle.heat_index_f,
        apparent_temp_f=bundle.apparent_temp_f,
        wind_mph=bundle.wind_mph,
        antecedent_precip_24_72h=bundle.antecedent_precip_24_72h,
        nws_products_available=bundle.sources_ok.get("nws") is not False,
        domain_complete=bundle.watershed_complete,
        dry_party=dry_party,
    )
