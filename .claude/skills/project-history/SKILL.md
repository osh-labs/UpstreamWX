---
name: project-history
description: Index of UpstreamWX's design rationale, security-audit remediations, data-quality hardening, deploy changes, and feature changelogs under docs/. Use when you need the "why" behind an existing subsystem — before changing ingest, the engine, confidence, caching, the API auth/budget layer, the PDF path, the CSP/frontend vendoring, or anything in deploy/ — or when a code comment, threshold YAML, or CLAUDE.md line cites an SA-XX finding, an FR-XX/NFR-X requirement, an issue number, or a dated changelog.
---

# UpstreamWX project history

The root `CLAUDE.md` carries current state only. The reasoning behind that state lives in
`docs/`. Read the relevant document **before** modifying the subsystem it covers — these
passes encode non-obvious constraints (trust boundaries, systemd ordering facts, failure
modes that read as benign) that the code alone will not teach you.

**Standing invariant:** every pass below preserved engine output bit-identically (NFR-4).
A change that alters engine output is a deliberate, separately-justified act.

## Governing documents

| Document | Covers |
|---|---|
| `UpstreamWX-PRD-v0.8.md` | Behavior, content, the `FR-XX`/`NFR-X` numbers cited throughout the code |
| `roadmap.md` | Build sequence, milestones M0.0–M0.5 |
| `frontend/STYLE_GUIDE.md` | Authoritative for PWA visual details |
| `docs/deployment-workflow.md` | Env ladder, branch protection, tag promotion, rollback, PWA cache busting |

## Milestone rationale

- `docs/m0.0/README.md` + `spike-{a..f}-*.md` — the de-risk spikes. Spikes E (REFS) and F (GEFS)
  de-risked the SREF+HREF EOL transition; `docs/m0.0/resource-profile.md` sizes the host.
- `docs/m0.1/README.md` … `docs/m0.4/README.md` — per-milestone findings.

## Security audit and remediation (2026-07-14 audit)

Source finding list: `docs/Security Audit 2026-07-14.md`. Each remediation has a workplan
(`docs/sa-*-workplan.md`) and, where it shipped separately, a changelog.

| Finding | Subject | Workplan / changelog |
|---|---|---|
| SA-01 | Anonymous fair-use sessions: stateless HMAC principal, HttpOnly cookie, per-principal + global budgets. Gate is secret-gated (`auth_active`) so dev/CLI/tests run open. | `docs/SA-01-public-auth-workplan.md` |
| SA-02 | Mission-input & cache bounds: strict `HazardInputsSpec`, body caps, byte-budget caches, miss-rate limiting. | `docs/sa-02-hardening-workplan.md` |
| SA-03 / SA-04 | Recurring-refresh work bounds (last-seen TTL, pass budget, shared semaphore) and cache-key isolation (mission metadata folded into the key). | `docs/sa-03-04-hardening-workplan.md`, `docs/changelog-2026-07-15-sa-03-04.md` |
| SA-05 | Vendored MapLibre + enforcing CSP (`script-src 'self'`); PDF template script externalized. | `docs/sa-05-vendor-map-libs-csp-workplan.md`, `docs/changelog-2026-07-17-sa-05.md` |
| SA-06 / SA-09 / SA-13 | `uv.lock` + `uv sync --frozen`, removal of root-executes-service-user-writable-code, trusted hosts + HTTPS deploy gate, healthcheck-URL log redaction. | `docs/sa-06-09-13-hardening-workplan.md`, `docs/changelog-2026-07-17-sa-06-09-13.md` |
| SA-08 | PDF/Chromium render containment: per-path body caps, bounded response fields, hardened flags. | `docs/sa-08-hardening-workplan.md`, `docs/changelog-2026-07-18-sa-08.md` |
| Host residuals (issue #132) | Atomic root-owned `releases/<sha>` + `current` symlink with health-check rollback, versioned TLS blocks + certbot, Chromium sandbox restored, signed-tag verification + SBOM CI. | `docs/changelog-2026-07-18-issue-132-host-hardening.md`, `docs/changelog-2026-07-19-issue-132-staging-validation.md` |

## Data quality (2026-07-02)

`docs/code-review-2026-07-02.md` is the review; three changelogs carry the fixes:
`changelog-2026-07-02-data-quality.md`, `-high-fixes.md`, `-gefs-resilience.md`.

The governing principle: **a missing, stale, NaN, or partial input must never read as benign.**
It is load-bearing across zonal aggregation (`None`, never NaN; no off-grid fallback), GEFS
member quorum and freshness bounds, tri-state precip booleans, `bundle_data_gaps()` → the SITREP
"DATA GAPS" section, the engine's explicit "DATA GAP … unassessed, not low" drivers, and
`confidence.yaml`'s `missing_primary_confidence` floor. GRIB2 framing is validated before a
subset is cached (`validate_grib2_bytes`) so a truncated publish degrades one member, not the source.

## Deploy and hosting

- `docs/changelog-2026-07-20-staging-deploy-hardening.md` (issues #146/#147/#148) — explicit
  `DEPLOY_CONFIG`, env-scoped `uwx-ctl uninstall`, unreadable-cache-root degradation. **Key
  systemd fact:** `EnvironmentFile=` always overrides `Environment=` regardless of line order,
  so the data-dir pin lives inside `ExecStart` via `/usr/bin/env`.
- Runbooks: `docs/prod-rebuild-runbook-v0.7.1.md`, `docs/staging-rebuild-runbook-2026-07-20.md`,
  `docs/prod-release-runbook-v0.7.0.md`.
- Two 2026-08-03 defects worth knowing: watershed delineation used to write its HyRiver cache to
  the CWD (unwritable under the read-only release tree — delineation failed while the briefing
  still rendered, an empty basin reading as benign); and the deploy layer never ran
  `systemctl enable`, so a provisioned box would not survive a reboot.

## Feature changelogs

- `docs/changelog-2026-07-19-hourly-hazard-series.md` — display-only `HazardSeries` plus
  time-aware per-phase `HazardInputs`. Flash flood is **deliberately** left window-conservative
  (upstream routing lag, §16.1); the engine never reads the display series.
- Unit localization (US customary / metric) — conversion is display-only; engine, thresholds,
  and all `HazardInputs` stay in native units.
- `docs/changelog-2026-08-06-planner-layout-install-flow.md` — narrow-phone mission-planner
  layout fix and the Add-to-Home-Screen flow. Two facts that get re-broken: a `1fr` grid track
  floors at its item's min-content width, and a `datetime-local`'s is wide enough to push a field
  off-screen (hence `minmax(0, …)` + `min-width: 0`); and **no API can open Safari's share menu**,
  so the iOS install is demonstrated, never triggered — `navigator.share()` is not a substitute.
- Domain split: app at `app.upstreamwx.com`, static landing from `landing/` at the apex;
  nginx blocks are `deploy/nginx/upstreamwx.conf` and `landing.conf`.
- `docs/font-vendoring-provenance.md` — vendored font licensing.
