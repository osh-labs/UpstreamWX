# SA-08 Workplan — Contain the PDF/Chromium render surface

- **Finding:** SA-08 — *Chromium runs without its native sandbox and accepts structurally broad input*
  (Medium) — `docs/Security Audit 2026-07-14.md` §SA-08.
- **Affected:** `src/upstreamwx/sitrep/pdf.py`, `api/app.py`, `api/models.py`,
  `deploy/systemd/upstreamwx-api.service`.
- **Status:** 🟡 The **in-repo, testable** parts are implemented and verified (full offline suite green,
  500 passed; the real PDF render exercised end-to-end with the hardened flags). The **native browser
  sandbox restoration** is host-dependent (systemd `RestrictNamespaces` interaction) and is deferred to
  the deploy/host pass (§4), tracked with the other host-only items.

---

## 1. The finding, against the code

`POST /v1/briefing/pdf` renders **client-supplied** `BriefingResponse` JSON in headless Chromium. It
already had good controls (2 MiB body threshold, a 2-render semaphore, per-IP rate limit + SA-01 budget,
HTML-escaping sub-models, a local-template allowlist, and a request-abort gate that blocks every browser
request except the template + its assets). The audit's three residuals:

1. **No native browser sandbox.** `pdf.py` launches Chromium with `--no-sandbox` because the systemd
   unit's `RestrictNamespaces=true` blocks the user namespace the renderer sandbox needs — so a Chromium
   renderer vulnerability has no in-browser containment.
2. **Structurally broad input.** Several `BriefingResponse` fields are broad strings / lists / dicts with
   no list-cardinality or nested-string limits (`models.py` — `markdown`, `warnings`, `sources_ok`,
   `metrics`, `timeline`, `hazard_detail`, `resources`, the `*_series`/GeoJSON dicts).
3. **Non-streaming body check.** The handler calls `await request.body()` and *then* checks the length —
   for a chunked upload that omits `Content-Length`, the whole body is buffered before the size check.

---

## 2. What this PR changes (done, offline-verified)

### #3 — streaming body reject on the PDF path (`api/app.py`)
The `_MaxBodySizeMiddleware` (SA-02) now takes a **per-path cap map** and the PDF path is folded in at its
own 2 MiB cap. The middleware is pure-ASGI and counts the streamed body, aborting with 413 **mid-stream**
— so a chunked upload without `Content-Length` can't buffer unbounded before the check. The handler's own
`await request.body()` check stays as defense-in-depth (now redundant behind the middleware).

### #2 — bound the broad response fields (`api/models.py`)
Every broad `BriefingResponse` field carries a **generous** cap — orders of magnitude above any legitimate
server-built briefing (cf. `sample-briefing.json`: `markdown` ~2.5 KB, every list ≤ 6) yet bounding a
hostile payload: `markdown` ≤ 256 KiB, `summary` ≤ 4000, scalar tokens ≤ 32–256, `warnings` ≤ 64 items ×
≤ 500 chars, `sources_ok` ≤ 64 keys × ≤ 64-char keys, `bluf`/`phases` ≤ 16, `metrics`/`hazard_detail`/
`resources` ≤ 64, `timeline` ≤ 256, `data_quality`/`*_series` dict cardinality ≤ 64. The **same model
validates the server's own `to_structured` output**, so the caps are deliberately above real output — the
frozen contract still validates (regression-tested) and the deeply-nested arbitrary GeoJSON/series dicts
are bounded by the 2 MiB body cap rather than per-field. An over-cap field is now a bounded **422** at the
endpoint, never a 500 or a render.

### #1 (partial) — trim the render's attack surface (`sitrep/pdf.py`)
Added safe headless-hardening launch flags — `--disable-gpu`, `--disable-extensions`,
`--disable-background-networking`, `--disable-sync` — alongside the existing `--no-sandbox` /
`--disable-dev-shm-usage` / `--disable-crash-reporter`. The render was exercised end-to-end to confirm the
flags don't break output. A comment documents *why* `--no-sandbox` is present and the path to restoring
the sandbox (§4).

---

## 3. Tests

`tests/test_api_pdf_hardening_sa08.py`: the frozen contract still validates; ten broad-field payloads
(oversized lists, nested strings, dict cardinality) each raise `ValidationError`; an over-cap field on the
endpoint is a clean 422; a chunked over-cap body is 413'd mid-stream by the middleware without reaching the
handler; and the PDF path is wired at the 2 MiB cap. The existing `test_pdf_export.py` /
`test_api_models.py` stay green (the 2 MiB 413 test now passes via the middleware). Engine output unchanged
(NFR-4).

---

## 4. Deferred — needs the live host (specified, not done here)

- **Restore Chromium's native sandbox** (the audit's primary recommendation). Options, both host-dependent:
  relax the systemd unit's `RestrictNamespaces` to permit the user namespace Chromium's sandbox needs (and
  drop `--no-sandbox`), or move PDF rendering into a **separately contained service/container** (no network,
  read-only fs, minimal readable paths, resource quotas, no secrets). Flipping `--no-sandbox` blind risks
  the renderer failing to launch on the host, so this is validated on the host, not in the container.
- **Disable JavaScript for the template** — *not feasible*: the template renders by reading
  `window.__BRIEFING__` in page JS, so disabling JS yields a blank PDF. Recorded so it isn't re-attempted.

These join the SA-06/09 host-only items already tracked for the deploy pass (issue #132).
