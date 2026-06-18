# M0.2 — SITREP Output as `.md` via Terminal: Findings

**Date:** 2026-06-18 · **Branch:** `claude/m0-2-planning-t1n521`

M0.2 adds the product's first user-facing output layer: a single terminal command that
turns a mission spec (point, date, window, cave/canyon) into a complete Markdown
briefing following the Appendix A skeleton (PRD §15), with the reference-only disclaimer
embedded from day one (roadmap §M0.2).

It follows the roadmap's mandated two-stage split so the engine stays validatable
independent of LLM variability:

1. **Structured render** (`upstreamwx/sitrep/render.py`) — deterministic Markdown from
   the engine's `BriefingResult`. Golden-file testable: identical inputs → byte-identical
   output (NFR-4). The language model never runs here and can never change a posture.
2. **Haiku framing** (`upstreamwx/sitrep/frame.py`) — optional natural-language summary
   (FR-21) added *above* the structured BLUF. Strictly constrained to narrate the
   structured object; every authoritative line stays byte-for-byte intact below it (FR-20).

## Exit-criteria status

| Exit criterion (roadmap §M0.2) | Status |
| --- | --- |
| Structured render passes golden-file tests | ✅ `tests/test_sitrep_render.py` + `tests/fixtures/sitrep/*.md` (canyon+HREF, benign cave) |
| Same inputs → byte-identical output | ✅ `test_render_is_deterministic` |
| Framed output preserves all postures unchanged | ✅ `test_sitrep_frame.py` (mocked) re-checks structured block verbatim; live FR-20 guard under `-m network` |
| Every briefing carries the disclaimer and source links | ✅ `test_render_carries_disclaimer_and_sources`; `DISCLAIMER` embedded in every render |
| Offline tests pass with no network; lint clean | ✅ `pytest` 112 passed / 10 network deselected; `ruff` clean |
| Live Haiku framing verified | ⏸ Pending an `ANTHROPIC_API_KEY` in the environment — covered by the network-gated `test_live_haiku_preserves_all_postures`. |

## What was built

### SITREP package — `upstreamwx.sitrep`
- **`render.py`** — `render_md(result, *, upstream=None, bundle=None, generated_at=None)`
  renders the §15 skeleton: header (HUC-12 domain), BLUF table, phase breakdown
  (thermal-primary marked, cave-isolation/no-lightning notes surfaced), per-hazard key
  drivers, upstream watershed summary, SOURCE DATA drill-down (SREF + the HREF same-day
  block when `bundle.href_in_range`, plus cross-ensemble agreement), notes
  (inferred-phases FR-9a, karst caveat), verify-source links, disclaimer. Reuses the
  engine enum `.label` / `severity_label` properties; no posture logic is re-derived.
- **`sources.py`** — `build_source_links(lat, lon, used_href=…)` builds the
  verify-against-NWS links (active alerts, point forecast/AFD) and reuses
  `sref.sources.NOMADS_BASE` / `href.sources.NOMADS_BASE` for the model-source links.
- **`frame.py`** — `frame_briefing(result, structured_md, *, client=None, model="claude-haiku-4-5")`
  serializes a compact view of the result, asks Haiku for a 2–4 sentence summary under a
  strict no-posture-change system prompt, and prepends it as a `## SUMMARY (plain language)`
  block. Lazy-imports `anthropic`; returns the structured render unchanged when no key.
- **`cli.py`** — the `upstreamwx` console entry point (argparse, mirroring the spike CLIs).

### Config & packaging
- `config.py`: `anthropic_api_key` (read from the standard `ANTHROPIC_API_KEY` via a
  `validation_alias` that bypasses the `UPSTREAMWX_` prefix).
- `pyproject.toml`: `anthropic` dependency + `[project.scripts] upstreamwx = …`.

## Usage

```sh
# Live end-to-end (NWS / Open-Meteo / SREF / HREF / watershed), framed if a key is set:
upstreamwx --lat 37.0192 --lon -111.9889 --activity canyon \
    --start 2026-06-20T08:00 --end 2026-06-20T18:00 --name "Buckskin Gulch"

# Offline / reproducible from a saved HazardInputs, structured render only:
upstreamwx --lat 37.0192 --lon -111.9889 --activity canyon \
    --start 2026-06-20T08:00 --end 2026-06-20T18:00 --slot \
    --inputs tests/fixtures/sitrep/sample_inputs.yaml --no-frame --out brief.md
```

Phase markers are optional (`--approach-end` / `--egress-start`); absent them the engine
infers approach = first hour, egress = last hour (FR-9a). Set `ANTHROPIC_API_KEY` in
`.env` to enable framing; `--frame` / `--no-frame` override the auto behavior.

Regenerate golden files after an intentional renderer format change:

```sh
.venv/bin/python tests/gen_sitrep_goldens.py
```
