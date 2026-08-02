# UpstreamWX — Technical Marketing Summary

*A briefing document for a writer producing an article about UpstreamWX. Everything
below is factual as of 2026-08-02 and traceable to the PRD (`UpstreamWX-PRD-v0.8.md`),
the roadmap (`roadmap.md`), and the codebase. Nothing here is aspirational unless the
section says so.*

---

## 1. One-paragraph version

**UpstreamWX** is a free, donation-supported progressive web app that produces a
mission-specific, multi-hazard weather briefing for **caving and canyoneering** trips
anywhere in the contiguous United States. Instead of a point forecast, it synthesizes
National Weather Service products, Open-Meteo derived fields, and in-house processing of
NOAA's **GEFS** and **REFS** ensemble GRIB2 data into a BLUF-format situation report
(SITREP) covering the four hazards that actually govern these sports: **flash flooding,
lightning, heat stress, and cold/wet hypothermia**. Its technical centerpiece is
**upstream-watershed aggregation** — precipitation and convective probability are
computed over the USGS hydrologic units that *drain into* the canyon or cave entrance,
not at the coordinate itself, because the rain that kills you in a slot canyon falls
where you cannot see it. UpstreamWX is explicitly **reference-only**: it presents a
structured hazard assessment with links to authoritative sources, and never issues a
go/no-go decision.

## 2. The problem it solves

A slot canyon or a cave is a hazard environment where the standard weather app is not
merely unhelpful — it is misleading.

- **Flash flooding** is driven by rain falling *outside the party's field of view*,
  often tens of miles upstream, frequently outside cell coverage. A point forecast at
  the trailhead can read clear while the drainage above is loading.
- **Lightning** matters most on exposed approach hikes, rim travel, and a slow egress —
  the phases where a party cannot quickly reach shelter — and is essentially irrelevant
  down in a slot or underground.
- **Heat stress** compounds with exertion and load on an exposed approach.
- **Cold/wet hypothermia** hits a soaked party exiting a canyon into falling evening
  temperatures — conditions a standard forecast presents as benign.

The existing landscape addresses none of this as a set. General weather apps (Windy,
Mountain-Forecast) give point forecasts with no watershed aggregation, no hazard logic,
and no mission awareness. Climbing tools are crag-oriented with no flood model. The only
watershed-aware product — the NWS Salt Lake City Southern Utah Flash Flood Outlook — is
a static regional graphic: flood only, no national coverage, no mission framing, no
drill-down.

The synthesis a meteorologically literate trip leader performs by hand — read the Area
Forecast Discussion, check convective timing, check ensemble probability and spread,
mentally aggregate precipitation over the upstream drainage, then separately reason
about lightning, heat, and post-exit cold — has no automated equivalent. UpstreamWX
automates exactly that synthesis.

## 3. The technical centerpiece: upstream-watershed aggregation

This is the part worth writing about at length.

1. The user drops a point on a map (long-press, geocoded address, pasted coordinates, or
   GPS).
2. The system resolves the containing **HUC-12** hydrologic unit from the USGS Watershed
   Boundary Dataset, then performs a **deterministic upstream trace** to assemble the
   full contributing area, plus a **pour-point delineation** for sub-HUC precision.
3. An optional user-set **Radius of Concern** (10/20/50/100/200 mi) clips that basin to a
   disk — a party can bound the domain to the drainage they consider relevant.
4. Ensemble precipitation and convective probability fields are then **zonally
   aggregated over that polygon** via GRIB2 `.idx` byte-range subsetting — the system
   downloads only the bytes for the fields and forecast hours it needs, never whole
   global files.

The trace carries **first-class completeness metadata**: it probes for external inflow at
every node, and an incomplete trace is surfaced as an explicit data gap that *caps* flash
flood confidence rather than being silently ignored.

**Lightning deliberately does not use the watershed.** Lightning is a point/corridor
hazard, not a basin-routed one, so its ensemble fields aggregate over a separate
**Lightning Area of Concern** — a disk around the activity with its own user-configurable
radius. Getting this distinction right is a substantive modeling decision, not a detail.

## 4. Phase-aware hazard modeling

A briefing is not one number for one place. The mission window is decomposed into
**approach → technical span → egress**, and the applicability matrix differs by activity:

| Phase | Canyon | Cave |
|---|---|---|
| **Approach** | Lightning + thermal, **heat weighted higher** (exposed terrain, midday exertion under load) | Same |
| **Technical span** | Flash flood (upstream watershed) + surface heat/cold. **Lightning does not apply** down in the slot. | Isolated from surface weather — **flash flood is the sole surface-derived risk**. Caves are thermally stable, so heat, cold, and lightning do not apply underground. |
| **Egress** | Lightning + thermal, **cold weighted higher** (a wet party exiting into cold air, wind, or a late-day temperature drop) | Same |

Each phase is now evaluated against **its own slice of the hourly forecast** rather than a
coarse window-wide estimate. Flash flood is a deliberate exception: it stays
window-conservative, because upstream rain arrives in-slot on a travel-time lag, and
slicing it by phase would understate it.

Overall posture is the **maximum** across phases and sources. A signal may raise a
posture relative to others; it may never silently lower one.

## 5. The data spine

| Source | Role |
|---|---|
| **NWS API** | Active flood/lightning products and alerts, Area Forecast Discussion. An active Flash Flood Watch/Warning already encodes a professional QPF-vs-flash-flood-guidance determination, so it serves as the near-term authoritative anchor. |
| **Open-Meteo** | Derived numerical fields — temperature, humidity, wind, CAPE, convective rate — and the 16-day hourly display series. |
| **SPC** | Convective outlooks. |
| **GEFS** (Global Ensemble Forecast System) | The coarse ensemble backstop beyond ~36 h. GEFS ships **no probability product**, so UpstreamWX computes **member-exceedance in-house** from per-member grids, with member fetches fanned across a worker pool. |
| **REFS** (~3 km RRFS Ensemble) | The same-day (~6–36 h) convection-allowing supplement. **Authoritative in-window** for both tier and confidence. Supplies native `LTNG` for lightning. |
| **USGS WBD / NHD** | Watershed boundaries and the upstream trace. |

Where both ensembles are in range, the engine takes the **higher tier**, and cross-source
agreement feeds the confidence model. Beyond REFS range, lightning falls back to a **GEFS
CAPE × precipitation member-exceedance proxy**, because GEFS has no native thunderstorm
field.

**A note the article should probably mention:** this spine was *migrated mid-build*. NWS
Service Change Notice 26-47 retires both SREF and HREF — the ensembles the product was
originally prototyped on — on **2026-08-31**. Rather than ship on a dying feed, the team
re-proved the whole ensemble path against GEFS and REFS with live de-risk spikes before
the public beta. That is an unglamorous engineering decision with a good story in it.

## 6. The five non-negotiable design constraints

These are enforced in code and in tests, not just documented. They are the most
distinctive thing about how the product is built.

1. **Reference-only, never a verdict.** No code path may emit a go/no-go, an "all
   clear," or an "all systems go." The disclaimer ships in *every* user-facing artifact
   and is persistent and non-dismissible in the app.
2. **The deterministic engine owns every posture; the language model only frames.** A
   rule engine decides all hazard tiers, categories, and confidence levels. An optional
   Claude Haiku layer may *narrate* the structured result in plain language, but is
   architecturally forbidden from changing a posture — and the test suite asserts the
   structured block stays byte-identical when framing is applied. Identical inputs
   always produce identical engine output.
3. **Thresholds are data, not code.** Every hazard cut point lives in versioned YAML with
   a `provenance` block. The engine references config by key and never hard-codes a
   number. Tuning a threshold in the field is a config change, not a release.
4. **Providers are swappable behind an interface.** The engine never imports a data
   provider. Every source fills a common bundle, which is mapped into the engine's
   normalized feature vector. This is what made the SREF→GEFS migration tractable.
5. **Graceful degradation.** A non-mandatory source being down marks that input
   unavailable and continues. It never crashes the briefing.

## 7. Data quality as a first-class value

The core insight, enforced throughout: **a missing, stale, NaN, or partial input must
never read as benign.** Concretely —

- Zonal aggregates return "unknown," never a number, and refuse off-grid nearest-cell
  fallbacks.
- Precipitation booleans are **tri-state**: unknown ≠ dry, and an unknown conservatively
  applies the elevated band.
- Both ensembles enforce a **freshness bound**; the cache token tracks the newest
  *available* cycle, not the wall clock.
- GEFS tolerates per-member failures behind a **member quorum**, and truncated GRIB2
  subsets (fetched mid-publication) are detected by framing validation and re-fetched
  rather than poisoning the cache.
- The engine emits explicit **"DATA GAP … unassessed, not low"** drivers, and confidence
  is floored at Low when a hazard's primary driver was unavailable.
- Every gap flows from a single derivation source into both the SITREP's "DATA GAPS"
  section and the app's structured data-quality block.

## 8. What the user actually sees

A six-tab PWA — **Overview, Map, Hazards, Briefing, Forecast, Resources** — over a dark
briefing chrome.

- **BLUF first.** Posture and confidence at the top, drivers and timing second, full
  source data on drill-down.
- **A map** showing the mission point, the upstream watershed (with the clipped-away
  remainder hatched), the Radius of Concern ring, and the Lightning Area of Concern ring,
  over switchable topo/aerial/street basemaps.
- **Per-hazard cards** with an inline SVG chart graphing each hazard's driving quantity
  across mission time, over threshold bands.
- **The full Markdown SITREP**, rendered as formatted HTML, with an attribution banner
  when language-model framing is active.
- **A mission planner** — geocode an address, paste decimal or DMS coordinates, use GPS,
  long-press to drop and move a marker, set both radii.
- **Offline and export.** The last briefing persists locally and is labeled with its age;
  a server-side headless-Chromium PDF export produces a clean download, with a
  client-side print fallback when offline.
- **US customary or metric**, app-wide — a display-only conversion that never touches an
  engine input or a threshold.

## 9. Engineering notes a technical audience will care about

- **Python 3.11, `uv`, FastAPI, pydantic.** One shared generation core that the CLI and
  the API both route through, so the API cannot drift from the CLI.
- **A validation corpus as the engine's oracle.** Hand-built boundary cases per hazard
  plus documented historical event replays. Changing engine logic or a threshold means
  the corpus must still hold, or be deliberately and provably updated with rationale.
- **Golden-file rendering tests.** The Markdown SITREP is byte-compared against committed
  fixtures.
- **A hermetic, offline-by-default test suite** (~520 tests) running against committed
  fixtures, with live-service tests marked and deselected by default.
- **An offline replay path** (`--inputs`) that renders a full briefing from a pinned
  feature vector — no network, no secrets, fully deterministic.
- **Latency engineering.** Cold watershed delineation (3–15 s) was the dominant briefing
  latency, and pre-caching is futile because every coordinate yields a slightly different
  basin. The fix: the planner fires a debounced warm request *the moment coordinates
  change*, delineating in the background while the user finishes entering the mission,
  with a single-flight registry so a fast user's briefing *joins* the in-flight
  delineation instead of racing it. Separately, GEFS decodes are cropped to the domain
  bounding box at decode time, returning kilobyte arrays instead of 16.5 MB global grids —
  a fix that came directly from OOM-killing the production host.
- **A serious security posture for a free tool.** A full external audit was run before
  public release and its findings closed: anonymous per-client HMAC session tokens (no
  login, no personal data) so cost budgets attach to identity rather than a bare IP;
  per-principal and global rolling-window budgets; strict bodily caps and typed schemas on
  every public surface; map libraries vendored and exact-pinned with a strict
  `script-src 'self'` Content Security Policy; atomic root-owned release directories with
  health-check rollback; signed-tag verification and an SBOM in CI.

## 10. Status and positioning

Build milestones **M0.0–M0.4** are complete and validated: de-risk spikes, the
deterministic engine with thresholds and validation corpus, the watershed and ingest
layers, the CLI SITREP, the FastAPI service, and the PWA wired to the live API.
**M0.5** — PWA polish — is in progress, and the **v0.5.0 public beta** is cut at the end
of it. The app lives at `app.upstreamwx.com` with a static landing page at the apex.

Deferred by design: climbing support (a different hazard model), native apps, satellite
messenger integration, in-canyon real-time alerting, and any form of go/no-go output.

**The line to land the article on:** UpstreamWX is not a weather app with a caving skin.
It is a decision-support instrument built around one uncomfortable fact — in a slot
canyon, the weather that matters is the weather you cannot see — and around a discipline
that consumer weather products rarely accept: it will tell you what it does not know, and
it will never tell you to go.

---

## Appendix — quick facts for fact-checking

| | |
|---|---|
| **Name / host** | UpstreamWX — `upstreamwx.com` (app at `app.upstreamwx.com`) |
| **Form factor** | Progressive Web App. No native app, by design. |
| **Coverage** | Contiguous United States |
| **Activities** | Caving and canyoneering (climbing deferred to v2) |
| **Hazards** | Flash flooding, lightning, heat stress, cold/wet hypothermia |
| **Posture ladder** | Minimal / Elevated / High / Extreme — except heat, which uses the official **NWS Heat Index categories** |
| **Cost model** | Free, donation-supported |
| **Accounts** | None in v1. Missions stored client-side. |
| **Ensembles** | GEFS (global backstop) + REFS (~3 km, same-day authoritative), 00/06/12/18Z |
| **Language-model role** | Optional plain-language framing only. Cannot change a posture. |
| **Author** | Chris Lee |
