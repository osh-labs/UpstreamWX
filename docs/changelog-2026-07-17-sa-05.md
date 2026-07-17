# Changelog — 2026-07-17: SA-05 (vendor & pin the map libraries + add a Content-Security-Policy)

Closes the High finding [`docs/Security Audit 2026-07-14.md`](Security%20Audit%202026-07-14.md)
**SA-05** — *Mutable CDN JavaScript runs without integrity or CSP protection*. Workplan:
[`docs/sa-05-vendor-map-libs-csp-workplan.md`](sa-05-vendor-map-libs-csp-workplan.md).

**Engine output is unchanged (NFR-4):** this is a frontend-asset + nginx change plus one small backend
touch (the PDF renderer's file-URI allow-list). No threshold config, engine, corpus, or golden render is
affected.

Verification: full offline suite **481 passed** (17 network deselected), `ruff` clean; server-side PDF
render still produces a 216 KB document; three headless-Chromium CSP smoke passes (PWA + map init; forced
MapLibre map + blob worker; served `?print=1` PDF template) each reported **0 CSP violations**.

---

## Vendored, exact-pinned, same-origin map libraries (fixed)

`frontend/index.html`, `frontend/vendor/`

The three jsDelivr resources — two at a **floating `@5`** major, none with integrity — are replaced by
same-origin vendored copies:

| Was (CDN, floating/no-SRI) | Now (same-origin, exact) |
|---|---|
| `maplibre-gl@5/dist/maplibre-gl.css` | `vendor/maplibre-gl-5.24.0.css` |
| `maplibre-gl@5/dist/maplibre-gl.js` | `vendor/maplibre-gl-5.24.0.js` |
| `maplibre-contour@0.1.0/dist/index.min.js` | `vendor/maplibre-contour-0.1.0.js` |

`crossorigin=""` is dropped (no longer cross-origin). Same-origin + `script-src 'self'` removes the
third-party host from the trust path entirely — strictly stronger than CDN + SRI and satisfying the
"works with CDN blocked" acceptance test for free. Exact pins and provenance hashes are recorded in the
workplan §5. The dangling `sourceMappingURL` comment was stripped from the vendored JS (its `.map` is not
vendored).

## PDF template inline script externalized (fixed)

`frontend/pdf/briefing-pdf.html`, `frontend/pdf/briefing-pdf.js` (new), `src/upstreamwx/sitrep/pdf.py`

The print/PDF template carried a ~400-line inline `<script>`. Under the enforced `script-src 'self'` that
would be blocked on the served **`?print=1` offline-fallback** path (the template *is* reachable over
nginx). The logic is moved verbatim into a sibling `briefing-pdf.js` referenced as `<script src>`. The
server-side renderer (`sitrep/pdf.py`, headless Chromium over `file://`) gates every request to an
allow-list; the new sibling file is added to `_allowed_request_paths` so the server render still loads it
(verified: 216 KB output). No behavior change to the PDF itself.

## Service-worker precache (fixed — acceptance #1)

`frontend/sw.js`

The three vendored assets and `pdf/briefing-pdf.js` are added to `SHELL_ASSETS`, so the map and PDF export
keep working **offline and when third-party CDNs are blocked**. The cache namespace stays release-stamped
(`sw.js?v=<release>`), so clients pick up the new precache list on the next deploy with no manual version
bump.

## Content-Security-Policy (new)

`deploy/nginx/upstreamwx.conf`, `deploy/nginx/landing.conf`

An **enforcing** CSP is added alongside the existing `add_header … always;` security headers.

- **App site** — `default-src 'self'` with strict **`script-src 'self'`** (no CDN, no `unsafe-inline`, no
  `unsafe-eval`). The map's unavoidable data planes are narrowly enumerated in `connect-src`/`img-src`
  (`tiles.openfreemap.org`, `server.arcgisonline.com`, `s3.amazonaws.com`) plus the Nominatim geocoder in
  `connect-src`; `worker-src 'self' blob:` (+ `child-src`) for MapLibre's blob worker; `style-src 'self'
  'unsafe-inline'` for MapLibre's runtime-injected control styles and the app's dynamic inline meter
  widths (the documented, script-src-preserving compromise the audit anticipated).
- **Landing site** — fully static and self-contained, so maximally strict: `script-src 'none'; style-src
  'self'`.

Both header lines carry an inline note flagging the overlap with PR B (SA-06/SA-09), which rewrites the
same server blocks for the `:443`/TLS listener.

---

## Files changed

- `frontend/index.html` — reference vendored same-origin map libs; drop `crossorigin`.
- `frontend/vendor/maplibre-gl-5.24.0.{js,css}`, `frontend/vendor/maplibre-contour-0.1.0.js` — new vendored, exact-pinned assets.
- `frontend/pdf/briefing-pdf.html` — inline `<script>` → external `<script src>`.
- `frontend/pdf/briefing-pdf.js` — new; the externalized template logic.
- `frontend/sw.js` — precache the vendored assets + `briefing-pdf.js`.
- `src/upstreamwx/sitrep/pdf.py` — allow the sibling `briefing-pdf.js` in the server render's file-URI gate.
- `deploy/nginx/upstreamwx.conf`, `deploy/nginx/landing.conf` — add the CSP header.
- `docs/sa-05-vendor-map-libs-csp-workplan.md`, `docs/changelog-2026-07-17-sa-05.md` — this remediation's docs.

## Not in scope

Does not address SA-06/SA-09 (deployment/TLS — PR B, in flight, same nginx blocks). Does not change the
engine, thresholds, corpus, or golden renders (NFR-4).
