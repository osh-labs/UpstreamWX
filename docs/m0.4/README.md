# M0.4 — PWA Framework: Map Location In → SITREP Out: Findings

**Date:** 2026-06-18 · **Branch:** `claude/prd-roadmap-backend-frontend-bi4tc9`

M0.4 wires the existing PWA shell to the live backend so a user picks a point and sees a
rendered SITREP (roadmap §M0.4; FR-1, FR-33, FR-38, FR-39, FR-40). The frontend was already
built well past a shell — five views, Leaflet map, glossary, About page — but rendered a
static `frontend/data/sample-briefing.json` and never called the API.

The gap was not "flip a fetch URL": the M0.3 API returned a **markdown-centric** payload,
while the PWA consumes a **rich structured object**. The bulk of M0.4 is a backend
serializer that maps the engine result + ingest bundle onto exactly that contract, so all
five views light up on live data. None of it depends on the production EC2.

## What shipped

- **Structured serializer** (`src/upstreamwx/sitrep/structured.py`) — `to_structured()`
  maps `BriefingResult` + `IngestBundle` onto the PWA's JSON contract. It is the API
  analogue of `render.py` (which produces Markdown): pure, deterministic, and decides
  nothing — every tier/category/confidence is the engine's verbatim output (FR-13, FR-20,
  NFR-4). The committed `frontend/data/sample-briefing.json` is the **frozen contract**
  (and the SW offline fallback / test oracle).
- **Static per-hazard logic copy** (`src/upstreamwx/sitrep/hazard_copy.py`) — the Hazards
  view's threshold-ladder definitions, citing Appendix B. Descriptive prose, **not** a
  threshold number (the numbers stay data in `data/thresholds/*.yaml`, FR-20a).
- **Hourly display series** (`IngestBundle.forecast_hourly`, populated by the Open-Meteo
  adapter from the same query that feeds the engine) — drives the Forecast table, the
  temp/wind charts, and the Overview metric cards. Display-only; never an engine input.
- **Response + serving** — `BriefingResponse` gains the structured fields (additive;
  `markdown` retained for the CLI). `app.py` serves the PWA single-origin via a
  `StaticFiles` mount (no CORS), resolved from the repo `frontend/` or
  `UPSTREAMWX_FRONTEND_DIR`.
- **Frontend wiring** — `loadBriefing()` POSTs `/v1/briefing`; a minimal mission editor
  (activity, window, party, slot — FR-33) and the map "move point" action rebuild the spec
  and re-fetch live, so the upstream watershed re-traces and re-renders. Graceful fallback
  to the bundled sample when no backend is reachable (offline / static-only hosting).

## Exit-criteria status

| Exit criterion (roadmap §M0.4) | Status |
| --- | --- |
| A user drops a point, the watershed traces and renders, and a correct SITREP appears | ✅ Verified live in-container: `POST /v1/briefing` ran full NWS/Open-Meteo/SPC/SREF/HREF/USGS ingest, traced the upstream basin (2.7 mi² polygon), localized the hourly forecast, and populated all five views; `sources_ok` all true, not degraded |
| Decision-ownership and disclaimer present (FR-39, FR-40) | ✅ Persistent reference-only disclaimer renders on live data (unchanged render layer) |
| Same content as the CLI / engine (FR-13) | ✅ One generation core + one serializer; `test_api.py::test_api_matches_cli` still holds (markdown), `test_structured.py` asserts postures are verbatim |
| Contract stable | ✅ `test_structured.py` / `test_api.py` assert the serialized shape equals `sample-briefing.json`; offline `inputs` path degrades to stable nulls (NFR-6) |

## Tests

- `tests/test_structured.py` — serializer shape == contract, postures tracked verbatim,
  severity-class vocabulary, watershed GeoJSON + forecast/metrics populated, graceful
  offline nulls.
- `tests/test_api.py` — response carries the structured contract; PWA served at `/`.
- Full suite + corpus + goldens unchanged and green (engine and `render.py` untouched).

## Notes / deferrals

- **Timezone label**: `mission.timezone` is a UTC-offset label (e.g. `UTC-04:00`) derived
  from the window offset — no tz-name database in the stack. Forecast hour labels are
  localized to that offset. A named-zone label (e.g. `EDT`) would need a lat/lon→tzname
  lookup; deferred.
- **M0.1.1** (EC2): recurring SREF scheduler cadence + cross-restart persistent cache.
- **M0.5**: offline-cache timestamp UX (FR-26/41), PDF export (FR-27), FR-35 color sign-off.
