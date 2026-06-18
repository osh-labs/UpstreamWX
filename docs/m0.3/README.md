# M0.3 — API Functional, Passing Internal Validation: Findings

**Date:** 2026-06-18 · **Branch:** `claude/m0-3-roadmap-89jm4c`

M0.3 wraps the deterministic engine and the M0.2 SITREP behind an HTTP API, with the
server-side caching and cycle-aligned regeneration the PRD assumes (PRD §7, §11; FR-12),
and graceful degradation when a non-mandatory source is down (NFR-6) — roadmap §M0.3.

The endpoint returns the **same briefing the CLI does** for the same inputs because both
now drive one shared generation core (`upstreamwx.sitrep.generate.generate_briefing`):
the API does not re-implement any of the mission → ingest → engine → render → frame path,
so it cannot drift from the CLI or alter a posture (FR-13).

## Exit-criteria status

| Exit criterion (roadmap §M0.3) | Status |
| --- | --- |
| API returns briefings identical in content to the CLI for the same inputs | ✅ `test_api.py::test_api_matches_cli` — API output == `cli.main` output (sole time-varying header line normalized) |
| Cache hit/miss behaves correctly | ✅ `test_api.py::test_cache_hit_on_reopen`, `test_api_cycles.py::test_cache_hit_miss_by_cycle` — hit on reopen, miss on new SREF cycle / new key |
| Scheduled refresh works | ✅ `test_api_cycles.py` — cycle boundary math + `refresh_active` regenerates in-range missions and drops ended ones (FR-12). The *always-on loop cadence + cross-restart persistence* are EC2-validated (M0.1.1), as for the SREF scheduler |
| Validation corpus passes through the API path | ✅ `test_api.py::test_validation_corpus_through_api` — every flash-flood boundary case routed through the service yields the engine's posture, rendered byte-identically to the engine path |
| Graceful degradation when a non-mandatory source is down (NFR-6) | ✅ `degraded` / `sources_ok` surfaced from the ingest bundle; the orchestrator already renders with the missing input marked unavailable rather than failing |
| Offline tests pass with no network; lint clean | ✅ `pytest` 163 passed / 12 network deselected; `ruff` clean |

## What was built

### Shared generation core — `upstreamwx.sitrep.generate`
- **`generate_briefing(mission, *, inputs=None, frame=None, generated_at=None, cycle=None)`**
  — the M0.2 CLI body extracted verbatim into one function returning a
  `GeneratedBriefing` (markdown + `BriefingResult` + bundle + warnings + `degraded`).
  `cli.py` and the API both call it, so identical inputs → identical content by
  construction. `inputs` skips live ingest and renders from a saved feature vector
  (offline / reproducible — the corpus path and FR-25 determinism).

### API package — `upstreamwx.api`
- **`app.py`** — FastAPI app. `POST /v1/briefing` (mission spec → briefing, structured +
  framed, cached) and `GET /v1/health` (liveness + current/next cycle + cache size). A
  `lifespan` starts the refresh scheduler for the app's lifetime; `main()` is the
  `upstreamwx-api` console entry (`uvicorn upstreamwx.api.app:app`).
- **`models.py`** — `MissionSpec` request (mirrors the CLI flags; optional `inputs`
  feature vector and `frame` override) and `BriefingResponse` (markdown + postures +
  `generated_at` + `cached`/`cache_cycle` + `degraded`/`sources_ok`/`warnings`).
- **`service.py`** — `BriefingService`: cache-aware generation, the active-mission
  registry, and the `refresh_active` pass. Serves a cached briefing when valid for the
  current cycle, else generates and caches; registers in-range **live** missions for
  scheduled refresh (deterministic offline briefings need none).
- **`cache.py`** — `BriefingCache`, an in-process, thread-safe store keyed by
  location/window/activity (`mission_cache_key`, coords rounded to ~11 m). Entries are
  valid for one SREF cycle; explicit-inputs briefings are deterministic and use a
  `static` token that never expires (FR-25). Cross-restart persistence is deferred to
  M0.1.1 (EC2) — the `get`/`put` interface is what a persistent backend implements.
- **`cycles.py`** — pure SREF-cycle arithmetic (03/09/15/21Z): `current_cycle`,
  `next_cycle`, `cycle_key`, `seconds_until_next_cycle`. Cache validity and the scheduler
  both key off these (FR-12).
- **`scheduler.py`** — `run_scheduler`, the thin asyncio loop that sleeps to each cycle
  boundary and calls `BriefingService.refresh_active`. The host-independent machinery
  (boundary math + one refresh pass) is unit-tested; the always-on cadence is EC2 work.

### Config & packaging
- `config.py`: `api_enable_scheduler` (default on; `UPSTREAMWX_API_ENABLE_SCHEDULER=0`
  runs the API without the background loop — used by tests and worker-less deploys).
- `pyproject.toml`: `fastapi` + `uvicorn[standard]` dependencies; `upstreamwx-api`
  console script.

## Scope boundary with M0.1.1

Like the SREF scheduler before it (roadmap §M0.1.1), the parts of M0.3 that genuinely
depend on an always-on host — the recurring refresh **cadence** running unattended and a
cache that **survives a restart** — cannot be validated in the ephemeral dev container.
What ships and is tested here is the host-independent core: the endpoint, the cache
semantics, the cycle arithmetic, and a single refresh pass. The asyncio loop that drives
the pass on a real clock, and a persistent cache backend, slot into the same interfaces
on the EC2 instance.

## Usage

```sh
# Serve the API (always-on backend; PRD §7):
upstreamwx-api            # or: uvicorn upstreamwx.api.app:app --reload

# Request a briefing (live ingest, framed if ANTHROPIC_API_KEY is set):
curl -s localhost:8000/v1/briefing -H 'content-type: application/json' -d '{
  "lat": 37.0192, "lon": -111.9889, "activity": "canyon",
  "start": "2026-06-20T08:00", "end": "2026-06-20T18:00", "name": "Buckskin Gulch"
}'

# Offline / reproducible (skip live ingest by pinning a HazardInputs feature vector):
curl -s localhost:8000/v1/briefing -H 'content-type: application/json' -d '{
  "lat": 37.0192, "lon": -111.9889, "activity": "canyon", "slot": true,
  "start": "2026-06-20T08:00", "end": "2026-06-20T18:00", "frame": false,
  "inputs": {"sref_p_precip": 65, "measurable_precip": true}
}'
```
