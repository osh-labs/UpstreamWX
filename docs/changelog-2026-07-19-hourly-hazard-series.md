# Changelog — Hourly hazard series + time-aware phase assessment (2026-07-19)

Per-hazard hourly forecast series on every hazard card, and phase assessments that respond to
the *hourly* forecast instead of the coarse approach/egress time estimate. Three increments;
engine determinism preserved throughout (NFR-4), reference-only ethos intact.

## Motivation

The engine collapsed each forecast field to one window aggregate (window-max precip/heat, window-min
apparent temp) *before* assessing, then evaluated that single vector against all three phases — so an
8 am approach and a 6 pm egress were scored against the *same* midday-worst numbers. The hazard cards
showed a scalar posture with no sense of *when* within the window risk peaked. The providers already
computed per-forecast-hour arrays and threw them away after the `max()` collapse.

## Increment 1 — capture the per-hour series (display-only)

Retain the per-forecast-hour arrays the providers discard, carry them on the bundle, and emit them in
the structured contract. Engine boundary preserved: `to_hazard_inputs` is unchanged and test-locked
bit-identical with/without the series populated (FR-13, NFR-4).

- `ingest/gefs_provider`, `refs_provider`: keep per-valid-time exceedance % alongside the window-max
  scalars.
- `ingest/openmeteo`: expose hourly heat index; add the `hours_dt` mission-clock axis on
  `ForecastHourly`.
- `ingest/base`: `HazardSeries` carrier + `build_hazard_series()` — resamples the sparse ensemble hours
  onto the dense mission clock (6 h GEFS / 3 h REFS step-hold, per-hour `max` merge with REFS
  authoritative in-window), gaps as `None` never `0`. Orchestrator builds it after all branches merge.
- `sitrep/structured`: per-hazard `series` block (`primary` / `secondary` / `bands`); heat & cold
  threshold bands read from `heat.yaml` / `cold_wet.yaml`, never hard-coded.
- `api/models`: bounded `SeriesLine` / `SeriesBand` / `HazardSeriesBlock` / `HazardDetail` sub-models.

## Increment 2 — the graph on each hazard card

Self-contained inline SVG (CSP `script-src 'self'` preserved; no new vendored libs).

- `lineChart()` gains threshold bands, per-series styling (bold ensemble / faint overlay), null-gap
  breaks, and a y-axis unit label — back-compatible with the two existing Forecast-tab calls.
- `renderHazards` draws a per-card chart: flash flood = ensemble line + faint hourly precip overlay;
  lightning = ensemble-only; heat / cold/wet = index line over threshold bands. `flushChartInits()`
  wired into the Hazards view; graceful "series unavailable" fallback. The "coarser resolution" caption
  note shows only on the ensemble-driven cards (flash flood / lightning).

## Increment 3 — phase assessment responds to the hourly forecast

`ingest/base.to_phase_hazard_inputs()` builds a per-phase `HazardInputs` by reducing the **local**
hazards over each phase's own forecast hours; `assess()` takes an opt-in `phase_inputs` and evaluates
each phase against its slice. `generate_briefing` threads it on the live path only.

- **Heat, cold/wet, lightning are time-sliced** (max heat / coldest apparent temp / max lightning over
  the phase window) — the morning approach and evening egress are no longer scored against the midday
  peak.
- **Flash flood is deliberately left window-conservative.** It is upstream-watershed-routed (§16.1):
  rain that fell upstream during the approach arrives in-slot on a travel-time lag, so narrowing it to
  the in-slot hours would *understate* it. Its card still graphs the hourly evolution (Increment 1);
  only its posture math stays window-wide.
- A phase with no hourly coverage for a field **falls back to the window value** (conservative — never
  a new data gap or a spurious confidence floor).

### Semantics / determinism

- `phase_inputs=None` (the offline `--inputs` path, the corpus, every legacy caller) is byte-identical
  to prior behavior — the whole offline suite and the golden SITREP render are unchanged.
- Because the phases tile the window contiguously, the overall FR-19 `max` is **unchanged for heat and
  cold/wet** (their peak is captured by whichever phase contains it).
- The overall max can only **lower** via **lightning**, and only when its peak falls in the technical
  span the party is sheltered through — where lightning is not applicable anyway (FR-14c). This removes
  a real artifact (the old window-max leaked a midday storm onto the exposed approach/egress phases) and
  was an explicit, approved product decision.

## Tests

- `tests/test_hazard_series.py` — Increment 1 merge/alignment + the `to_hazard_inputs` bit-identity lock.
- `tests/test_phase_inputs.py` — per-phase reduction + the flash-flood carve-out, uncovered-phase
  fallback, no-axis → `None`, phase divergence in `assess`, and `phase_inputs=None` identical to default.
- Full offline suite green (512 passed), ruff clean. Frontend verified via headless-Chromium render of
  the real `sample-briefing.json` (4 charts, 0 CSP violations).
</content>
