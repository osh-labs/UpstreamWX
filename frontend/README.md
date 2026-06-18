# frontend/ — UpstreamWX PWA

The UpstreamWX Progressive Web App. Behavior and information architecture are
governed by the PRD (§6.8, Appendix D §18); the **visual source of truth** is
[`STYLE_GUIDE.md`](STYLE_GUIDE.md). Roadmap: M0.4 (map location in → SITREP out)
and M0.5 (full five-view IA, offline cache, PDF export).

## What's here

A dependency-free, installable PWA shell that implements the dark briefing chrome
from the reference mockups, adapted to the PRD (four hazards; the
Minimal/Elevated/High/Extreme ladder; phase-primary hazards; upstream-watershed
map overlay; reference-only posture — see `STYLE_GUIDE.md §2` for the full diff).
No build step or Node toolchain: it runs from static files so it installs and
works offline as-is.

```
STYLE_GUIDE.md          design source of truth (tokens + components ↔ PRD FRs)
index.html              app shell: header · tab bar · 5 views · status · ack modal
manifest.webmanifest    installable PWA metadata
sw.js                   service worker — offline shell + last-briefing cache (FR-26/28)
styles/tokens.css       design tokens (mirror of STYLE_GUIDE.md §3–§6)
styles/app.css          component styles (STYLE_GUIDE.md §7)
js/app.js               view renderers + bootstrap (PRD §6.8 / Appendix D)
js/icons.js             inline SVG icon set (no icon-font dependency)
icons/icon.svg          maskable brand mark
data/sample-briefing.json  offline sample mirroring BriefingResponse/BriefingResult
```

## Run it

```sh
cd frontend
python3 -m http.server 8080
# open http://localhost:8080/
```

The five views — **Overview · Forecast · Map · Hazards · Resources** (FR-32) —
render from `data/sample-briefing.json`, which mirrors the backend contract
(`src/upstreamwx/api/models.py` `BriefingResponse` + `engine/models.py`
`BriefingResult`). The first-run reference-only acknowledgment (FR-31) shows once.

## Wiring to the backend (M0.4)

The only change to go live is in `js/app.js` `loadBriefing()`: replace the
`fetch('data/sample-briefing.json')` with `POST /v1/briefing` (the M0.3 API),
passing the mission spec. The render layer, the design tokens, and the service
worker's offline-cache behavior are unchanged — the sample JSON is shaped exactly
like the API response. Mission persistence (FR-10) and the live map layer
(Leaflet/MapLibre over the schematic SVG in `renderMap`) are the remaining M0.4
wiring.
