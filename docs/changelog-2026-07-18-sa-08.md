# Changelog — 2026-07-18: SA-08 (contain the PDF/Chromium render surface)

Advances SA-08 from [`docs/Security Audit 2026-07-14.md`](Security%20Audit%202026-07-14.md) (Medium —
*Chromium runs without its native sandbox and accepts structurally broad input*). Workplan:
[`docs/sa-08-hardening-workplan.md`](sa-08-hardening-workplan.md).

The **in-repo, testable** parts land here; the **native browser sandbox restoration** is host-dependent
(systemd `RestrictNamespaces`) and is deferred to the deploy pass (workplan §4, issue #132). Full offline
suite green (**500 passed**), ruff clean, and the real PDF render exercised end-to-end with the hardened
flags. **Engine output unchanged (NFR-4).**

---

## Streaming body reject on the PDF path (audit #3) — fixed

`src/upstreamwx/api/app.py`

`_MaxBodySizeMiddleware` (SA-02) now takes a **per-path cap map**, and `/v1/briefing/pdf` is folded in at
its own 2 MiB cap. Because the middleware counts the streamed body and aborts with 413 **mid-stream**, a
chunked upload that omits `Content-Length` can no longer be fully buffered by the handler's
`await request.body()` before the size check. The handler's own check remains as defense-in-depth.

## Bound the broad response fields (audit #2) — fixed

`src/upstreamwx/api/models.py`

The `/v1/briefing/pdf` endpoint renders client-supplied `BriefingResponse` JSON in headless Chromium, so
every broad field now carries a **generous** cap — orders of magnitude above any legitimate server-built
briefing (`sample-briefing.json`: `markdown` ~2.5 KB, every list ≤ 6) yet bounding a hostile payload's
list cardinality and string sizes: `markdown` ≤ 256 KiB, `summary` ≤ 4000, scalar tokens ≤ 32–256,
`warnings` ≤ 64 × ≤ 500 chars, `sources_ok` ≤ 64 keys, `bluf`/`phases` ≤ 16, `metrics`/`hazard_detail`/
`resources` ≤ 64, `timeline` ≤ 256, `data_quality`/`*_series` cardinality ≤ 64. The same model validates
the server's own `to_structured` output, so the frozen contract still validates (regression-tested); the
deeply-nested GeoJSON/series dicts are bounded by the 2 MiB body cap. An over-cap field is now a bounded
**422**, not a 500 or a render.

## Trim the render's attack surface (audit #1, partial) — fixed

`src/upstreamwx/sitrep/pdf.py`

Added safe headless-hardening Chromium flags (`--disable-gpu`, `--disable-extensions`,
`--disable-background-networking`, `--disable-sync`) and a comment explaining why `--no-sandbox` is present
(the systemd `RestrictNamespaces` block) and the host-dependent path to restoring the in-browser sandbox.
The render was exercised end-to-end to confirm the flags don't break output.

**Still open (deferred, host pass — workplan §4, issue #132):** restore Chromium's native sandbox (relax
`RestrictNamespaces` + drop `--no-sandbox`, or isolate rendering in a separate hardened container). Disabling
JS for the template is *not feasible* (the template renders via `window.__BRIEFING__` in page JS).

New tests: `tests/test_api_pdf_hardening_sa08.py`.
