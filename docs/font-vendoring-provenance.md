# Font vendoring provenance

UI fonts were converted from the system stack to **Albert Sans** (body + headings) and
**Ubuntu Mono** (all monospace), vendored same-origin under `frontend/vendor/fonts/` and
`landing/vendor/fonts/` (identical copies — the PWA and the static landing site are served
from separate nginx roots per `deploy/nginx/upstreamwx.conf` / `landing.conf`, so there is
no shared static path between them). Same self-hosting rationale as the SA-05 map-library
vendoring: no CDN dependency, no third-party request from the app origin, works offline,
precached by `frontend/sw.js` (landing has no service worker). No nginx CSP change was
needed — both configs already send `font-src 'self'`.

## Files

| File | Source | Version | License |
|---|---|---|---|
| `albert-sans-v4-latin-wght-normal.woff2` | Google Fonts "Albert Sans" (variable, weight 100–900), via `@fontsource-variable/albert-sans@5.3.0` | v4 (per `fonts.gstatic.com/s/albertsans/v4/...`) | SIL Open Font License 1.1 |
| `ubuntu-mono-v19-latin-400-normal.woff2` | Google Fonts "Ubuntu Mono", regular, via `@fontsource/ubuntu-mono@5.3.0` | v19 (per `fonts.gstatic.com/s/ubuntumono/v19/...`) | Ubuntu Font License 1.0 |
| `ubuntu-mono-v19-latin-700-normal.woff2` | Google Fonts "Ubuntu Mono", bold, via `@fontsource/ubuntu-mono@5.3.0` | v19 | Ubuntu Font License 1.0 |

Only the `latin` subset is vendored (matches the product's CONUS-only, English-only scope).
Albert Sans is vendored as a single variable-weight file rather than discrete static faces
so the type scale's non-multiple-of-100 weights (620, 650 — see `STYLE_GUIDE.md` §4) render
at their exact value instead of snapping to the nearest static face.

The Ubuntu Font License 1.0 (unlike the SIL OFL that Albert Sans and most Google Fonts use)
requires that a *modified* font be renamed — not applicable here, as the files are
unmodified subsets fetched via the `@fontsource`/`@fontsource-variable` npm packages, which
redistribute the upstream Google Fonts files verbatim.

`@font-face` rules live in `frontend/styles/tokens.css` and `landing/styles/tokens.css`
(the latter a vendored copy of the former, per that file's own header comment).
