"""API request/response schema (M0.3).

The request is a mission spec mirroring the CLI arguments, with an optional saved
``HazardInputs`` feature vector for offline/reproducible generation (the corpus path,
FR-25). The response carries the rendered Markdown briefing plus the cache/cycle and
source-availability provenance the PWA (M0.4) needs to show currency and degradation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from ..engine.models import ActivityType, Hazard, HazardInputs, Mission
from ..timezones import localize_window

# ---------------------------------------------------------------------------------------
# Request-validation bounds (H-8): the public API must reject requests it could never
# brief *before* burning ingest cost (watershed delineation, GEFS/REFS decode).
# ---------------------------------------------------------------------------------------
# CONUS bounding box: the product is contiguous-US-only (PRD §2) — NWS coverage, HUC-12
# watersheds, and the REFS 3 km domain all end at these edges, so a point outside them can
# only produce a degraded briefing at full ingest cost.
CONUS_LAT_MIN, CONUS_LAT_MAX = 24.0, 50.0
CONUS_LON_MIN, CONUS_LON_MAX = -125.0, -66.0
# Max mission window length. Multi-day expeditions are real; beyond a week the GEFS/REFS
# ensemble horizon can't cover the window anyway, so a longer window is a malformed request.
MAX_WINDOW_DAYS = 7
# The window must START within the GEFS horizon: the 0.25° product ends at f240 (10 days).
MAX_START_LEAD_DAYS = 10
# A live window fully ended more than this long ago is a stale briefing request, not a plan.
MAX_ENDED_AGE_H = 24
# Upper bound for both radii: 322 km ≈ 200 mi, the PRD's documented max slider stop (FR-3).
MAX_RADIUS_KM = 322.0


class MissionWindowError(ValueError):
    """A live mission window falls outside the serviceable horizon (maps to HTTP 422, H-8)."""


def _cmp_utc(value: datetime) -> datetime:
    """Normalize to a naive-UTC datetime for window arithmetic.

    Mission windows are entered as *local wall-clock* naive datetimes (FR-9); the checks
    below are coarse abuse bounds (days/hours), so treating naive values as UTC — at most
    ~8 h of CONUS skew — is deliberately good enough and keeps validation zone-free.
    """
    return value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)


def _reject_markup(v: str) -> str:
    """Refuse angle brackets in short display tokens.

    These fields are clock windows / cycle ids the PDF template (FR-27) interpolates into
    HTML; legitimate values never contain markup, so any ``<``/``>`` is a hostile payload
    aimed at the headless-Chromium renderer and is rejected at the model boundary.
    """
    if "<" in v or ">" in v:
        raise ValueError("markup characters are not allowed in this field")
    return v


# A short local clock window ("1400–2100") or hour token ("1300"): bounded and markup-free
# so the PDF template can never be handed HTML where it expects a time string.
ClockToken = Annotated[str, Field(max_length=24), AfterValidator(_reject_markup)]
# A short identifier-ish display token (ensemble cycle id, SPC category).
ShortToken = Annotated[str, Field(max_length=32), AfterValidator(_reject_markup)]


# ---------------------------------------------------------------------------------------
# Strict engine-input request model (SA-02). The offline replay path (FR-25) previously
# accepted ``inputs: dict`` and expanded it with ``HazardInputs(**data)`` — an unknown key
# became a 500 (TypeError) and NaN/inf/out-of-range floats flowed straight into the engine.
# The constrained scalar aliases below plus :class:`HazardInputsSpec` reject all of that at
# the model boundary while reproducing the exact engine dataclass, so deterministic offline
# replays stay bit-identical (NFR-4, FR-25).
# ---------------------------------------------------------------------------------------
# Percent probability [0, 100] over the upstream/lightning domain (engine convention).
Prob = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
# Unit interval [0, 1] — per-hazard ensemble member support (FR-17).
Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
# Any finite float (temperatures, rates, CAPE, wind): reject NaN/inf, leave the range open.
FiniteF = Annotated[float, Field(allow_inf_nan=False)]


class HazardInputsSpec(BaseModel):
    """Strict request mirror of the engine :class:`HazardInputs` dataclass (SA-02).

    Reproduces **every** field of ``engine.models.HazardInputs`` with identical defaults,
    but strictly validated: ``extra='forbid'`` turns an unknown key into a bounded 422 (not
    a 500 from ``HazardInputs(**data)``), every float rejects NaN/inf, and probabilities are
    clamped to their real domains. :meth:`to_dataclass` yields the exact engine dataclass, so
    offline replays and the validation corpus reproduce bit-identically (NFR-4, FR-25).
    """

    model_config = ConfigDict(extra="forbid")

    # Active NWS products (FR-5); defaults mirror the dataclass exactly.
    flash_flood_warning: bool = False
    flash_flood_watch: bool = False
    flood_warning: bool = False
    flood_advisory: bool = False
    flood_watch: bool = False
    thunderstorm_warning: bool = False
    nws_products_available: bool = True

    # GEFS ensemble aggregates over the upstream domain.
    gefs_p_precip: Prob | None = None
    gefs_p_tstm: Prob | None = None
    measurable_precip: bool | None = False          # tri-state: None = unknown (preserved)
    convective_rate_in_per_hr: FiniteF | None = None
    cape_jkg: FiniteF | None = None

    # REFS same-day high-resolution overlay (FR-7a).
    refs_p_precip: Prob | None = None
    refs_p_lightning: Prob | None = None

    # Confidence inputs (FR-17): member_support keyed by Hazard.value, each in [0, 1].
    member_support: dict[str, Unit] = Field(default_factory=dict, max_length=8)
    source_agreement: str = Field(default="consistent", max_length=16)

    # SPC/AFD signals.
    spc_category: str | None = Field(default=None, max_length=32)
    afd_storm_mode: str | None = Field(default=None, max_length=32)
    afd_flood_mention: bool = False

    # Open-Meteo derived fields (deg F / mph).
    heat_index_f: FiniteF | None = None
    apparent_temp_f: FiniteF | None = None
    wind_mph: FiniteF | None = None
    antecedent_precip_24_72h: bool | None = False   # tri-state: None = unknown (preserved)

    # Data-quality / activity modifiers.
    domain_complete: bool = True
    dry_party: bool = False

    @field_validator("member_support")
    @classmethod
    def _known_hazards(cls, v: dict[str, float]) -> dict[str, float]:
        """Reject member_support keys that are not valid :class:`Hazard` values (SA-02)."""
        bad = set(v) - {h.value for h in Hazard}
        if bad:
            raise ValueError(f"unknown hazard keys in member_support: {sorted(bad)}")
        return v

    def to_dataclass(self) -> HazardInputs:
        """Expand to the engine dataclass unchanged — bit-identical replays (NFR-4, FR-25)."""
        return HazardInputs(**self.model_dump())


class MissionSpec(BaseModel):
    """A mission briefing request (mirrors the `upstreamwx` CLI flags).

    Structural validity (window ordering/length, CONUS bounds, radius caps) is enforced at
    the model boundary (H-8). Wall-clock currency checks live in :meth:`ensure_current` —
    called by the service with its injectable ``now`` — so deterministic offline replays
    (``inputs`` supplied, FR-25) never expire as real time passes.
    """

    lat: float = Field(
        ge=CONUS_LAT_MIN, le=CONUS_LAT_MAX, description="contiguous-US only (PRD §2)"
    )
    lon: float = Field(
        ge=CONUS_LON_MIN, le=CONUS_LON_MAX, description="contiguous-US only (PRD §2)"
    )
    activity: ActivityType
    start: datetime = Field(description="window start (ISO 8601)")
    end: datetime = Field(description="window end (ISO 8601)")
    name: str = Field(default="mission", max_length=80)
    approach_end: datetime | None = Field(default=None, description="phase marker (FR-9a)")
    egress_start: datetime | None = Field(default=None, description="phase marker (FR-9a)")
    party_size: int | None = Field(default=None, ge=1, le=200)
    route_note: str | None = Field(default=None, max_length=1000)
    slot: bool = False
    radius_km: float | None = Field(
        default=None,
        ge=1,
        le=MAX_RADIUS_KM,
        description="Radius of Concern (km): caps the upstream watershed; null = unbounded (FR-3)",
    )
    lightning_radius_km: float | None = Field(
        default=None,
        ge=1,
        le=MAX_RADIUS_KM,
        description=(
            "Lightning Area of Concern radius (km): aggregate the lightning signal over a disk "
            "around the activity instead of the watershed; null = upstream domain (PRD §16.1)"
        ),
    )
    frame: bool | None = Field(
        default=None,
        description="add Haiku framing; null = frame iff ANTHROPIC_API_KEY is set (FR-21)",
    )
    inputs: HazardInputsSpec | None = None

    @field_validator("inputs", mode="before")
    @classmethod
    def _unwrap_envelope(cls, v: object) -> object:
        """Accept the corpus/CLI ``{"inputs": {...}}`` envelope before strict validation (SA-02).

        The offline replay path (FR-25) sometimes wraps the feature vector in an outer
        ``inputs`` key; unwrap that single-key envelope so the inner object is validated as a
        :class:`HazardInputsSpec`. Any other value passes through unchanged.
        """
        if isinstance(v, dict) and set(v) == {"inputs"} and isinstance(v["inputs"], dict):
            return v["inputs"]
        return v

    @model_validator(mode="after")
    def _validate_window(self) -> MissionSpec:
        """Reject malformed windows before any work is spent on them (H-8).

        Deterministic checks only (no wall clock), so an offline replay spec with pinned
        dates validates identically forever (NFR-4).
        """
        start, end = _cmp_utc(self.start), _cmp_utc(self.end)
        if end <= start:
            raise ValueError("mission window end must be after its start")
        if end - start > timedelta(days=MAX_WINDOW_DAYS):
            raise ValueError(
                f"mission window exceeds {MAX_WINDOW_DAYS} days — beyond a week the "
                "GEFS/REFS ensemble horizon cannot cover the window"
            )
        return self

    def ensure_current(self, now: datetime | None = None) -> None:
        """Reject live windows outside the serviceable forecast horizon (H-8).

        Raises :class:`MissionWindowError` (→ HTTP 422) when the window starts beyond the
        GEFS f240 horizon or ended more than :data:`MAX_ENDED_AGE_H` ago. Windows already
        underway are fine. Skipped entirely for ``inputs``-supplied specs: those are
        deterministic replays of a saved feature vector (FR-25), and a wall-clock check
        would invalidate them as real time passes.
        """
        if self.inputs is not None:
            return
        now_utc = _cmp_utc(now if now is not None else datetime.now(UTC))
        if _cmp_utc(self.start) > now_utc + timedelta(days=MAX_START_LEAD_DAYS):
            raise MissionWindowError(
                f"mission window starts more than {MAX_START_LEAD_DAYS} days out — beyond "
                "the GEFS forecast horizon (f240); request the briefing closer to the trip"
            )
        if _cmp_utc(self.end) < now_utc - timedelta(hours=MAX_ENDED_AGE_H):
            raise MissionWindowError(
                f"mission window ended more than {MAX_ENDED_AGE_H} hours ago — a briefing "
                "for a past window is stale; adjust the window to a current or upcoming trip"
            )

    def to_mission(self) -> Mission:
        # The window is entered as local wall-clock time at the trip point; attach the
        # point's IANA zone so the engine's UTC math and the local-time display agree (FR-9).
        start, end, approach_end, egress_start = localize_window(
            self.lat, self.lon, self.start, self.end, self.approach_end, self.egress_start
        )
        return Mission(
            activity_type=self.activity,
            lat=self.lat,
            lon=self.lon,
            window_start=start,
            window_end=end,
            approach_end=approach_end,
            egress_start=egress_start,
            party_size=self.party_size,
            route_note=self.route_note,
            is_slot=self.slot,
            name=self.name,
            radius_km=self.radius_km,
            lightning_radius_km=self.lightning_radius_km,
        )

    def to_inputs(self) -> HazardInputs | None:
        """Expand the validated ``inputs`` spec to the engine dataclass (FR-25)."""
        return self.inputs.to_dataclass() if self.inputs is not None else None


class WatershedWarmRequest(BaseModel):
    """A request to pre-warm the pour-point watershed cache for a point (FR-3).

    Sent by the mission planner the moment coordinates change so the upstream basin is
    delineated in the background while the user finishes entering the mission. Only the
    point matters: ``radius_km`` is accepted for symmetry with :class:`MissionSpec` but is
    ignored, since the Radius-of-Concern clip runs after delineation.

    Coordinates carry the same CONUS bounds as :class:`MissionSpec` (H-8): a warm outside
    the product's coverage would spend a 3-15 s USGS delineation on a point that can never
    be briefed.
    """

    lat: float = Field(
        ge=CONUS_LAT_MIN, le=CONUS_LAT_MAX, description="contiguous-US only (PRD §2)"
    )
    lon: float = Field(
        ge=CONUS_LON_MIN, le=CONUS_LON_MAX, description="contiguous-US only (PRD §2)"
    )
    radius_km: float | None = Field(
        default=None, ge=1, le=MAX_RADIUS_KM, description="ignored for delineation"
    )


class MissionView(BaseModel):
    """The ``mission`` block of the structured contract (sample-briefing.json).

    Typed just tightly enough that the PDF renderer (FR-27) can trust the fields it
    interpolates: coordinates are real numbers, names/labels are bounded strings. Extra
    keys (``huc12``, ``radius_km``, ``phases_inferred``, …) pass through untouched so the
    frozen contract is preserved verbatim.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(default="", max_length=200)
    activity: str = Field(default="canyon", max_length=32)
    is_slot: bool = False
    lat: float = Field(default=0.0, ge=-90, le=90)
    lon: float = Field(default=0.0, ge=-180, le=180)
    window_start: str | None = Field(default=None, max_length=64)
    window_end: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    tz_name: str | None = Field(default=None, max_length=64)


class BlufEntry(BaseModel):
    """One hazard row of the BLUF table (Overview view + PDF hazard summary)."""

    model_config = ConfigDict(extra="allow")

    hazard: str = Field(max_length=32)
    label: str = Field(max_length=32)
    severity_class: str = Field(max_length=32)
    confidence: str | None = Field(default=None, max_length=16)
    window: ClockToken | None = Field(
        default=None, description="local HHMM–HHMM window of concern; null = persistent"
    )
    is_persistent: bool = False


class PhaseCard(BaseModel):
    """One phase card (approach/technical/egress) of the structured contract (FR-9a)."""

    model_config = ConfigDict(extra="allow")

    phase: str = Field(max_length=32)
    window: ClockToken | None = None
    thermal_primary: str | None = Field(default=None, max_length=32)
    lead_label: str | None = Field(default=None, max_length=120)
    applicable: str | None = Field(default=None, max_length=200)
    # Generous cap: joined engine phase notes are sentences, not documents; the template
    # HTML-escapes the note, so the cap only bounds size.
    note: str | None = Field(default=None, max_length=2000)


class ForecastRow(BaseModel):
    """One labelled row of the hourly forecast table (display-only, never an engine input)."""

    model_config = ConfigDict(extra="allow")

    label: str = Field(max_length=32)
    # 512 comfortably covers the longest legitimate hourly series (Open-Meteo tops out at
    # 16 forecast days = 384 h) while still bounding a hostile payload.
    values: list[str | int | float | None] = Field(default_factory=list, max_length=512)


class ForecastTable(BaseModel):
    """The ``forecast_hourly`` table: hour tokens + per-metric rows (Forecast view + PDF)."""

    model_config = ConfigDict(extra="allow")

    hours: list[ClockToken] = Field(default_factory=list, max_length=512)
    rows: list[ForecastRow] = Field(default_factory=list, max_length=16)


class RiskInputsView(BaseModel):
    """Scalar engine inputs echoed for the Forecast view's Risk Analysis section (FR-20).

    Numbers are numbers and the two display strings are bounded, markup-free tokens: the
    PDF template interpolates these into HTML, so the model guarantees no field can
    smuggle markup into the headless render. Display-only — never re-read by the engine.
    """

    model_config = ConfigDict(extra="allow")

    # int | float (not bare float) so the server's rounded integer percentages keep their
    # exact wire format (65, not 65.0) — the contract stays byte-compatible.
    gefs_p_precip: int | float | None = None
    gefs_p_tstm: int | float | None = None
    refs_in_range: bool | None = None
    refs_p_precip: int | float | None = None
    refs_p_lightning: int | float | None = None
    refs_cycle: ShortToken | None = None
    cape_jkg: int | float | None = None
    convective_rate_in_per_hr: int | float | None = None
    spc_category: ShortToken | None = None
    flash_flood_warning: bool | None = None
    flash_flood_watch: bool | None = None
    flood_watch: bool | None = None
    thunderstorm_warning: bool | None = None


class BriefingResponse(BaseModel):
    """A generated (or cached) briefing and its provenance.

    Carries both the Markdown SITREP (``markdown`` — the CLI's artifact) and the
    structured view the PWA renders its five views from (M0.4). The structured fields are
    built by :func:`upstreamwx.sitrep.structured.to_structured`; their shape is the frozen
    contract in ``frontend/data/sample-briefing.json``. Every posture here is the engine's
    verbatim output — the response layer decides nothing (FR-13, FR-20).

    The fields the PDF renderer interpolates (``mission``, ``bluf``, ``phases``,
    ``forecast_hourly``, ``risk_inputs``) are typed sub-models rather than bare dicts:
    ``POST /v1/briefing/pdf`` accepts this schema from the client and renders it in
    headless Chromium, so the model is the first line of defence against briefing JSON
    carrying HTML where the template expects a number or clock window.
    """

    # SA-08: the PDF endpoint accepts this schema from a client and renders it in headless
    # Chromium, so every broad field carries a generous cap — orders of magnitude above any
    # legitimate server-built briefing (cf. sample-briefing.json: markdown ~2.5 KB, every list
    # <= 6) yet bounding a hostile payload's list cardinality and string sizes. Deeply nested
    # arbitrary dicts (watershed/roc/laoc GeoJSON, the *_series value lists) are bounded by the
    # endpoint's 2 MiB streaming body cap rather than per-field.
    markdown: str = Field(max_length=262_144)
    overall_posture: str = Field(max_length=32)
    overall_confidence: str = Field(max_length=32)
    # A semicolon-join of every threshold-config version (5 today, ~76 chars); generous headroom.
    threshold_version: str = Field(max_length=256)
    generated_at: datetime
    framed: bool
    cached: bool = Field(description="True if served from cache without regenerating")
    cache_cycle: str = Field(
        max_length=64,
        description="GEFS/REFS cycle token this briefing is current for (newest available run)",
    )
    degraded: bool = Field(description="True if a non-mandatory source was unavailable (NFR-6)")
    sources_ok: dict[Annotated[str, Field(max_length=64)], bool] = Field(
        default_factory=dict, max_length=64
    )
    warnings: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=64
    )
    data_quality: dict = Field(
        default_factory=dict,
        max_length=64,
        description=(
            "First-class availability/provenance: data gaps affecting this briefing plus the "
            "model cycles actually used (NFR-6). Display-only; never re-read by the engine."
        ),
    )

    # Structured view for the PWA (M0.4). See sample-briefing.json for the shape.
    # (hazard_detail / metrics / timeline / resources stay bare dicts: every value the PDF
    # template reads from them is HTML-escaped or mapped through a fixed lookup.)
    mission: MissionView = Field(default_factory=MissionView)
    watershed: dict | None = None
    roc: dict | None = Field(default=None, description="Radius-of-Concern ring; null = unbounded")
    laoc: dict | None = Field(
        default=None, description="Lightning-Area-of-Concern ring; null = upstream domain"
    )
    summary: str | None = Field(default=None, max_length=4000)
    bluf: list[BlufEntry] = Field(default_factory=list, max_length=16)
    metrics: list[dict] = Field(default_factory=list, max_length=64)
    phases: list[PhaseCard] = Field(default_factory=list, max_length=16)
    timeline: list[dict] = Field(default_factory=list, max_length=256)
    hazard_detail: list[dict] = Field(default_factory=list, max_length=64)
    forecast_hourly: ForecastTable = Field(default_factory=ForecastTable)
    temp_series: dict = Field(default_factory=dict, max_length=64)
    wind_series: dict = Field(default_factory=dict, max_length=64)
    risk_inputs: RiskInputsView = Field(
        default_factory=RiskInputsView,
        description="scalar engine inputs for the Forecast view (FR-20)",
    )
    resources: list[dict] = Field(default_factory=list, max_length=64)
