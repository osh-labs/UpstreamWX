# SA-05 Remediation Workplan — Vendor & pin the map libraries + add a Content-Security-Policy

- **Finding:** **SA-05** — *Mutable CDN JavaScript runs without integrity or CSP protection* (High) —
  [`docs/Security Audit 2026-07-14.md`](Security%20Audit%202026-07-14.md) §SA-05.
- **Target:** the public beta server (Internet-exposed) and every client that loads the PWA. The control
  is a **supply-chain + browser-policy** hardening: it removes third-party executable code from the app
  origin and constrains what the origin may load/execute/connect to.
- **Constraint (explicit):** **engine output is unchanged (NFR-4).** This is a frontend-asset + nginx
  change plus one small backend touch (the PDF renderer's file-URI allow-list). No threshold config, no
  engine, no corpus, no golden renders change.
- **Status:** ✅ **Implemented.** See §6 for the per-item done checklist and the changelog
  [`docs/changelog-2026-07-17-sa-05.md`](changelog-2026-07-17-sa-05.md).

---

## 1. The vulnerability, restated against the code

`frontend/index.html` loaded three third-party executable/style resources from jsDelivr, two of them at a
**floating major** specifier, none with a Subresource-Integrity hash, and nginx sent **no
Content-Security-Policy**:

| Resource | Before | Problem |
|---|---|---|
| `maplibre-gl.css` | `…/maplibre-gl@5/dist/maplibre-gl.css` | floating `@5`, no `integrity=` |
| `maplibre-gl.js` | `…/maplibre-gl@5/dist/maplibre-gl.js` | floating `@5`, **executable**, no `integrity=` |
| `maplibre-contour` | `…/maplibre-contour@0.1.0/dist/index.min.js` | exact but no `integrity=`, external host |
| CSP | *(absent)* | nothing constrained script/connect/img/worker origins |

An app-origin script can read persisted mission/briefing data from `localStorage`, request geolocation,
call the same-origin `/v1/*` API (billable endpoints), and change the displayed assessment. A compromised
CDN path, a malicious package publication, or mutable major-version resolution therefore executes
attacker-controlled code **inside the UpstreamWX origin**. A git tag does not freeze a floating CDN
dependency.

## 2. Acceptance criteria (from the audit)

1. A release works with third-party CDN access **blocked**.
2. Every unavoidable external executable resource has an **exact version + verified integrity hash**.
3. An **enforced CSP** produces no unexpected violations across the full PWA, map, service-worker, PDF,
   and offline flows.

## 3. Approach chosen — vendor same-origin (stronger than CDN + SRI)

Rather than pin-and-SRI a CDN, the map libraries are **vendored into the release and served same-origin**.
Same-origin + `script-src 'self'` is simpler and strictly stronger than CDN + SRI: there is no third-party
host in the trust path at all, and it satisfies acceptance #1 (CDN-blocked) for free. There is now **no
unavoidable external executable resource**, so acceptance #2 is met by construction (the exact pins and
their provenance hashes are recorded in §5 as the reviewed-artifact record).

### Vendored assets (`frontend/vendor/`)

Exact versions resolved from the floating specifiers at implementation time:

| File | npm pin | Source |
|---|---|---|
| `maplibre-gl-5.24.0.js` | `maplibre-gl@5.24.0` | `cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/maplibre-gl.js` |
| `maplibre-gl-5.24.0.css` | `maplibre-gl@5.24.0` | `…/maplibre-gl@5.24.0/dist/maplibre-gl.css` |
| `maplibre-contour-0.1.0.js` | `maplibre-contour@0.1.0` | `…/maplibre-contour@0.1.0/dist/index.min.js` |

The version is embedded in each filename so the pin is explicit at the reference site and a future bump is
a visible, reviewable rename. The dangling `//# sourceMappingURL=maplibre-gl.js.map` comment was stripped
from the vendored JS (the `.map` is not vendored; the comment would only 404 in devtools).

## 4. The Content-Security-Policy

### App site (`deploy/nginx/upstreamwx.conf`)

`script-src` stays **strict `'self'`** — the security-critical directive. The enumerated remote hosts are
the map's unavoidable data planes (they serve *data*, not executable code):

```
default-src 'self';
base-uri 'self';
object-src 'none';
frame-ancestors 'none';
form-action 'self';
script-src 'self';
style-src 'self' 'unsafe-inline';
img-src 'self' data: blob: https://server.arcgisonline.com https://s3.amazonaws.com https://tiles.openfreemap.org;
font-src 'self';
connect-src 'self' https://tiles.openfreemap.org https://server.arcgisonline.com https://s3.amazonaws.com https://nominatim.openstreetmap.org;
worker-src 'self' blob:;
child-src 'self' blob:;
manifest-src 'self'
```

Host enumeration (grepped from `frontend/js/app.js`):

| Host | Role | Directive |
|---|---|---|
| `tiles.openfreemap.org` | OpenMapTiles vector tiles + glyph PBFs (topo/street basemap) | `connect-src` (+ `img-src` for sprite safety) |
| `server.arcgisonline.com` | Esri World Imagery raster tiles (aerial basemap) | `connect-src` + `img-src` |
| `s3.amazonaws.com` | Terrarium DEM tiles (hillshade + `maplibre-contour` contours) | `connect-src` + `img-src` |
| `nominatim.openstreetmap.org` | address geocoder (search box) | `connect-src` |
| same-origin `/v1/*`, assets | API + PWA shell (relative paths) | `'self'` |

Attribution / resource links (`api.weather.gov`, `forecast.weather.gov`, `open-meteo.com`,
`donate.stripe.com`, `openfreemap.org`, …) are `<a href target=_blank>` **navigations**, not subresource
fetches, so they need no `connect-src` entry.

Two documented, deliberate relaxations:

- **`worker-src 'self' blob:` / `child-src 'self' blob:`** — MapLibre GL runs its render worker from a
  `Blob` URL (verified: `new Worker(URL.createObjectURL(new Blob(...)))` in the vendored dist). Omitting
  `blob:` silently blanks the map.
- **`style-src 'self' 'unsafe-inline'`** — MapLibre injects control stylesheets at runtime and the app
  renders dynamic inline `style=""` (meter/bar widths from data). `script-src` — the directive that
  actually stops code execution — stays strict; only style is relaxed. This is the pragmatic compromise the
  audit anticipated.

### Landing site (`deploy/nginx/landing.conf`)

The apex landing page is fully static and self-contained (same-origin CSS + an SVG icon, **no JavaScript,
no inline styles**), so it gets a maximally strict policy: `script-src 'none'; style-src 'self'`.

## 5. Reviewed-artifact record (integrity hashes)

SRI is not *used* (nothing is loaded cross-origin anymore), but the reviewed exact artifacts are recorded
here so a future re-vendor can be verified byte-for-byte:

```
# upstream jsDelivr dist (as downloaded)
maplibre-gl-5.24.0.js     sha384-5+cfbwT0iiub6VsQAdn6yz16nr6sDiQoHx6tm4O8OVYXHYOxcffFmCJBL0dgdvGp  (pre-sourcemap-strip)
# in-repo committed copies (verify these)
maplibre-gl-5.24.0.js     sha384-smUoTG/824+sEEmwCDM129Tt5ByvGeEfzXhPl1D7x7mH4QBXz2FESl1TFzrLHce5  (post-sourcemap-strip)
maplibre-gl-5.24.0.css    sha384-uTttxo/aOKbdE5RlD/SPzSDoDmNvGlUYPjONi2MN/b7c9HPSvW07OIuyP7uL6jxK
maplibre-contour-0.1.0.js sha384-rKd9nNV3F43xwxeA6WNNsPH+cMYQl6SEM+/HDoU/sgeuK+faXwhqXYA0Pwp0COXT
```

The JS has two hashes: the upstream dist as downloaded, and the committed copy after the one-line
`sourceMappingURL` comment strip (the only modification). CSS and contour are byte-identical to upstream.
Verify any copy with `openssl dgst -sha384 -binary FILE | openssl base64 -A`.

## 6. Done checklist (per deliverable)

- [x] Vendored exact-pinned `maplibre-gl` (JS+CSS) and `maplibre-contour` into `frontend/vendor/`, served
      same-origin (`index.html` references updated; `crossorigin=""` removed).
- [x] Externalized the PDF template's inline `<script>` → `frontend/pdf/briefing-pdf.js` so the served
      `?print=1` fallback satisfies strict `script-src 'self'`; added it to the server-side renderer's
      file-URI allow-list (`sitrep/pdf.py`).
- [x] Precached the three vendored assets + `briefing-pdf.js` in `frontend/sw.js` (offline / CDN-blocked
      still works — acceptance #1). Cache namespace remains release-stamped, so clients pick up the change
      on the next deploy.
- [x] Added an **enforcing** CSP to `deploy/nginx/upstreamwx.conf` (app) and a strict CSP to
      `deploy/nginx/landing.conf` (landing).
- [x] Verified: full offline `pytest` suite (**481 passed**, 17 network deselected), `ruff` clean,
      server-side PDF render (216 KB), and three headless-Chromium CSP smoke passes (PWA + map init;
      forced MapLibre map + blob worker; served `?print=1` PDF template) — **0 CSP violations** each.

## 7. Rollout note (Report-Only safety valve)

The policy ships **enforcing** because every flow was verified violation-free in a headless browser. If a
production surface surfaces an unforeseen violation, the low-risk rollback is to rename the header to
`Content-Security-Policy-Report-Only` (with a `report-uri`/`report-to` collector), observe, then flip back
to enforcing — no application code changes.

## 8. Coordination with PR B (SA-06 + SA-09)

PR B rewrites the same nginx `server` blocks to add the `:443`/TLS listener and the HTTP→HTTPS redirect,
touching the `add_header` region. The CSP `add_header` lines are placed alongside the existing
`add_header … always;` lines with an inline coordination comment; whoever merges second rebases and keeps
the CSP line in the TLS server block.
