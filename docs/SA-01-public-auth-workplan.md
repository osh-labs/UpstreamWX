# SA-01 Remediation Workplan — Public-Release Authentication & Fair-Use Gate

- **Finding:** SA-01 — *Private beta access is not technically restricted* (Security Audit 2026-07-14, High)
- **Scope of this plan:** the **public release** access/abuse gate. The private beta stays behind the tailnet (the audit's accepted interim mitigation); this plan builds the durable control that replaces "possession of the URL is the invitation."
- **Decisions locked (with the product owner):**
  1. **Anonymous fair-use sessions** — server-minted signed tokens, *no login and no personal data*. The gate exists to establish a **per-principal identity for cost/abuse accounting**, not to decide *who* gets in. This matches the audit's explicit public-release recommendation and preserves the "asks for no personal data" stance (see SA-10).
  2. **App-level, self-contained** — implemented in FastAPI + nginx only. No third-party IAP, no managed challenge service. Portable across tailnet / staging / the single EC2 host.

## Implementation status (2026-07-15)

**Phases 1–2 and the cheap SA-12 fold-ins are built, tested, and verified** on branch
`claude/sa-01-auth-plan-zc1p9h` (rebased on the merged SA-02 hardening):

- `api/auth.py` (stateless HMAC session tokens, `Principal`, `require_session`, cookie helpers)
  and `api/budget.py` (per-principal + global rolling-window counters) are new.
- `api/app.py` gained `_SessionMiddleware` (fail-closed by path), `POST /v1/session`, per-endpoint
  `require_session` + budget charging, lifespan fail-closed on a missing secret, docs-off (SA-12),
  and a loopback-default standalone bind (SA-12). `api/service.py` gates refresh registration per
  principal (SA-03). `config.py` gained the gate/session/budget settings (all default to a no-op).
- Frontend (`frontend/js/app.js`): transparent `ensureSession()` on boot + credentialed fetches +
  401 re-mint/retry. The service worker needs no change (it already bypasses non-GET).
- Tests: `tests/test_api_auth.py` + `tests/test_api_budget.py` (17 tests) cover all three acceptance
  criteria, token integrity, rotation, mint limiting, budgets, and SA-03. Full offline suite green
  (467 passed), ruff clean, and the gate verified end-to-end against a live uvicorn (401 without a
  session on a direct port hit → 200 after mint).
- Deploy: `deploy/upstreamwx.env.example` documents the gate/secret/budget vars; the nginx template
  adds a strict per-IP mint zone. Everything is behind `api_auth_enabled` (default OFF) — a reversible
  config flip, zero change to the tailnet beta or the test suite until enabled.

**Deferred (as planned):** the proof-of-work mint hardening (§5.8, GA-time, flag-off) and the
`/v1/health` field trim (SA-12) are not in this pass — the health trim would churn the SA-02 health
key-set test and is low-value next to the gate itself.

## 1. What "authentication" means here (and what it does not)

This product is a free, donation-supported PWA. We are **not** authenticating humans; we are authenticating *clients* with an opaque principal id so that fair-use budgets, cost ceilings, and abuse controls attach to a **stable, app-issued identity** instead of to a bare IP. The audit is explicit that "IP-only throttling is weak identity and is readily shared, rotated, or bypassed." The anonymous-session principal is the missing layer between the edge IP limiter (defense-in-depth, kept) and the expensive/billable work.

**This plan closes SA-01's three acceptance tests:**

| Acceptance test | How this plan satisfies it |
|---|---|
| An unauthenticated request to the PWA and each `/v1` endpoint is denied. | Every expensive `/v1/*` endpoint requires a valid session token; the PWA shell + JS load, then mint a session before the first live call. |
| An authorized beta user can complete the normal PWA, PDF, and framing flow. | The PWA mints a session transparently on boot; all four endpoints (`/v1/briefing`, `/v1/briefing/frame`, `/v1/briefing/pdf`, `/v1/watershed/warm`) succeed with it. |
| The control cannot be bypassed by reaching port 8000 directly. | Auth is enforced **in the app**, not in nginx. A direct hit on uvicorn:8000 without a valid token is still `401`. (An nginx-only Basic-Auth gate would fail this test.) |

**Explicitly out of scope of this plan** (separate findings, noted where they interact): SA-02 input bounds, SA-04 cache-key isolation, SA-06 deploy trust boundary. The session gate *reduces* but does not *replace* those; call-outs are in §9.

## 2. Architecture overview

```
                         ┌─────────────────────────── nginx (edge, defense-in-depth) ──────────────────┐
Browser (PWA)            │  limit_req 2r/s (kept) + new strict zone for /v1/session mint                │
  │  1. GET / (shell,    │  passes X-Forwarded-For; TLS terminates here (Secure cookie needs it, SA-09) │
  │     JS, assets)  ────┼──────────────────────────────────────────────────────────────────────────► │
  │  2. POST /v1/session │                                    uvicorn (ONE process — no workers)         │
  │     (mint) ──────────┼───────────────►  api/auth.py: mint HMAC token ── Set-Cookie: uwx_session      │
  │  3. POST /v1/briefing│                  api/app.py SessionMiddleware: allowlist + verify → Principal │
  │     (+cookie) ───────┼───────────────►  api/budget.py: charge(principal, ip, "cold") → allow/deny    │
  │                      │                  service.get_briefing(...) (unchanged core)                   │
  └──────────────────────┘                  _register_active gated to authorized principal + quota (SA-03)
```

- **Principal** = a random 128-bit id inside a **stateless, HMAC-SHA256-signed** token. No server-side session table needed (works even across a future restart or worker split; only the *counters* are process-local).
- **Transport** = an **HttpOnly, Secure, SameSite=Lax cookie** (`uwx_session`). HttpOnly means a compromised CDN script (SA-05) cannot read the token; same-origin `fetch` sends it automatically, so the frontend diff is minimal.
- **Enforcement** = a FastAPI **middleware** (global allowlist: everything under `/v1/*` except the mint + health endpoints requires a token) **plus** a per-route `Depends(require_session)` that hands the endpoint a typed `Principal`. Belt-and-suspenders so a newly added route can't ship unauthenticated by omission.
- **Accounting** = `api/budget.py` charges each expensive call against **three** rolling windows: per-principal (fairness), per-IP-aggregate (defeats token rotation), and global (absolute cost ceiling / circuit breaker). Cache **hits are free** — only cache-miss / billable work is charged, preserving the "reopening the app is free" property.

## 3. Threat model deltas this addresses

- **URL leak / crawler / distributed clients** → must first mint a session; minting is itself IP-rate-limited and (optionally, §5.8) proof-of-work-gated, and per-IP-aggregate budgets bound what any number of rotated tokens from one source can spend.
- **Model-cost abuse** (`/v1/briefing/frame` is billable) → per-principal daily model budget **and** a global daily model-spend ceiling with a circuit breaker (503) + alert.
- **Persistent workload amplification (SA-03)** → refresh registration happens only for an authorized principal and is capped per-principal well below the global 256.
- **Chromium-launch abuse (`/v1/briefing/pdf`)** → per-principal PDF budget on top of the existing `_pdf_sem` and per-IP token bucket.

## 4. Configuration surface (config.py)

Add to `Settings` (mirrors the existing `api_rate_limits_enabled` / cap-knob pattern; all default to values that keep the current tailnet beta and the offline test suite behaving exactly as today):

```python
# --- Public-release access gate (SA-01) --------------------------------------------
# Master switch. Default OFF so the tailnet beta and the hermetic test suite are
# unaffected; deploy sets it to 1 for the public host. When ON and no signing secret is
# configured, the app FAILS CLOSED at startup (refuses to serve) rather than issuing
# forgeable tokens.
api_auth_enabled: bool = False

# HMAC signing secret for anonymous session tokens. Read as UPSTREAMWX_SESSION_SECRET
# (32+ random bytes, e.g. `openssl rand -hex 32`); lives in the runtime EnvironmentFile,
# never in git. *_PREV allows zero-downtime secret rotation (verify-only).
session_secret: str | None = None
session_secret_prev: str | None = None
session_ttl_s: int = 7 * 24 * 3600          # 7 days; PWA re-mints transparently on expiry
session_mint_rate_per_min: float = 5.0      # per-IP mint budget (strict — minting is cheap for us)

# Per-principal fair-use budgets (cache HITS are never charged).
budget_cold_per_principal_per_hour: int = 20     # cache-miss briefings
budget_frame_per_principal_per_day: int = 30     # billable Anthropic calls
budget_pdf_per_principal_per_hour: int = 20
budget_warm_per_principal_per_hour: int = 60
budget_active_per_principal: int = 3             # SA-03: refresh registrations per principal

# Per-IP aggregate ceilings (defeat token rotation from one source).
budget_cold_per_ip_per_hour: int = 60
budget_frame_per_ip_per_day: int = 60

# Global ceilings / circuit breakers (absolute host + cost protection).
budget_global_cold_per_hour: int = 1200
budget_global_frame_per_day: int = 2000          # model-spend ceiling → 503 + alert when hit

# Optional stdlib proof-of-work on session mint (§5.8). Default OFF for beta; ON for GA.
session_pow_enabled: bool = False
session_pow_bits: int = 18                        # ~quarter-second of client CPU per token

# SA-12 fold-ins (access-control adjacent):
docs_enabled: bool = False                        # disable /docs,/redoc,/openapi.json in prod
```

**Fail-closed rule:** in `lifespan`, if `api_auth_enabled` and neither `session_secret` is set → log a fatal error and raise, so a misconfigured production host cannot boot issuing tokens signed with a dev-default key. In dev (auth disabled) an ephemeral per-process random key is fine.

## 5. Component design

### 5.1 `src/upstreamwx/api/auth.py` (new)

Stateless token, stdlib only (consistent with the dependency-free `_TokenBucketLimiter` already in `app.py`):

- `mint(secret, *, tier="anon", now, ttl) -> str`
  - payload = `{"v":1,"pid":<secrets.token_hex(16)>,"iat":<int>,"exp":<int>,"tier":tier}`
  - token = `b64url(json(payload)) + "." + b64url(hmac_sha256(secret, b64url_payload))`
- `verify(token, secrets: list[str], *, now) -> Principal | None`
  - split, recompute HMAC over the payload segment, `hmac.compare_digest` against each accepted secret (current + prev, for rotation), reject on mismatch/expired/malformed. Returns `Principal(pid, tier)` or `None`.
- `@dataclass(frozen=True) Principal: pid: str; tier: str`
- Cookie helpers: `set_session_cookie(response, token, *, ttl, secure)` → `Set-Cookie: uwx_session=<token>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=<ttl>`. `read_session_cookie(request)`.
- `require_session(request) -> Principal` FastAPI dependency: reads cookie (fallback: `Authorization: Bearer` for non-browser/API-token clients later), verifies, raises `HTTPException(401, "session required")` on miss/invalid.

**CSRF posture:** the sensitive endpoints are same-origin `fetch` POSTs with `content-type: application/json`. Cross-site JSON POSTs trigger a CORS preflight that we never answer (no `CORSMiddleware`), and `SameSite=Lax` blocks the cookie on cross-site sends anyway. As a cheap backstop, `require_session` also requires a static custom header `X-UWX-Session: 1` on state-affecting POSTs (a cross-origin page cannot set a custom header without CORS). No server-side CSRF token table needed.

### 5.2 Enforcement — middleware + dependency (app.py)

- Add `SessionMiddleware` (a small `@app.middleware("http")`): if `api_auth_enabled` and `request.url.path` starts with `/v1/` and is **not** in the allowlist (`/v1/health`, `/v1/session`, `/v1/session/challenge`), verify the session; on failure short-circuit with `401`. This guarantees coverage even for routes that forget the dependency.
- Keep `Depends(require_session)` on each expensive route so the handler receives the typed `Principal` for budget charging.
- Static PWA assets (the `/` catch-all `StaticFiles` mount) are **never** gated — the shell and JS must load so the JS can mint a session.

### 5.3 Session endpoints (app.py)

```
POST /v1/session            → mint. Per-IP rate-limited via a new _session_limiter
                              (reuse _TokenBucketLimiter, session_mint_rate_per_min).
                              If session_pow_enabled, require a valid PoW solution (§5.8).
                              200 {"ok": true} + Set-Cookie: uwx_session=...
POST /v1/session/challenge  → (only when PoW enabled) issue {nonce, bits}; nonce is an
                              HMAC-signed short-TTL token so the server stays stateless.
```

Minting binds nothing to the token beyond `pid` — IP correlation for budgeting is done at request time against the live connection IP (`_client_ip`, already implemented and XFF-safe), which is more robust than embedding an IP that legitimately roams on mobile/CGNAT.

### 5.4 `src/upstreamwx/api/budget.py` (new)

A thread-safe rolling-window counter, same shape and LRU-bounding as `_TokenBucketLimiter`:

- `class WindowCounter` with `charge(key, *, limit, window_s, now) -> int | None` → `None` when under budget, else whole seconds of `Retry-After`. Internally a fixed-window or token-bucket per key; the map is LRU-capped (`_RATE_LIMIT_MAX_IPS`-style) so it can't become a memory sink.
- `class BudgetEnforcer` wrapping the kinds. One call per expensive request:
  ```python
  def charge(self, kind, principal, ip, *, now=None) -> None:  # raises BudgetExceeded(retry_after)
      # checks, in order, three windows for `kind`:
      #   per-principal  key=f"{kind}:pid:{principal.pid}"   limit=<per_principal>
      #   per-ip         key=f"{kind}:ip:{ip}"               limit=<per_ip>
      #   global         key=f"{kind}:global"                limit=<global>  → GlobalCeiling
  ```
- `BudgetExceeded` → `429 + Retry-After`; `GlobalCeiling` → `503 + Retry-After` and a `logger.warning` (the alerting hook). Cache hits do not call `charge`.

Kinds: `cold`, `frame`, `pdf`, `warm`, `active` (registration).

### 5.5 Wiring the four endpoints (app.py / service.py)

- `POST /v1/briefing`: add `principal = Depends(require_session)`. Charge `cold` **only on a cache miss** — pass the principal into `service.get_briefing` (or charge in the handler around the miss path) so hits stay free. Existing `MissionWindowError`/`BriefingBusy` handling unchanged.
- `POST /v1/briefing/frame`: `require_session` + `budget.charge("frame", ...)` **before** the Anthropic call (in addition to the existing `_frame_limiter` per-IP bucket, which stays). 204-no-key path unchanged.
- `POST /v1/briefing/pdf`: `require_session` + `budget.charge("pdf", ...)` before acquiring `_pdf_sem`.
- `POST /v1/watershed/warm`: `require_session` + `budget.charge("warm", ...)`.
- **SA-03 fold-in (service.py):** `_register_active` becomes authorized-principal-gated. `get_briefing` receives the `Principal`; registration happens only when the principal is under `budget_active_per_principal`, and the registry entry records the owning `pid`. Anonymous/over-quota principals still get their one-shot briefing but create **no** recurring scheduler work. (Full SA-03 hardening — scheduler wall-clock/cost budget, registry lock — is tracked under SA-03; this plan delivers the "register only for authorized principals + per-principal quota" half.)

### 5.6 Frontend (frontend/js/app.js)

- `ensureSession()`: on boot (guarded by `!DEMO_MODE` like `warmWatershed`), `POST /v1/session` with `credentials: "same-origin"`. If `session_pow_enabled`, first fetch `/v1/session/challenge`, solve it (a ~10-line Web Worker so the UI never blocks), then mint. Memoize a "session ready" promise; `postBriefing`/`streamSummary`/`warmWatershed`/PDF all `await` it.
- Add `credentials: "same-origin"` and header `"X-UWX-Session": "1"` to the four existing `fetch` calls (`postBriefing` line ~437, `warmWatershed` ~471, `streamSummary` ~506, PDF export ~1902).
- **401 handling:** if any live call returns 401 (expired/rotated), re-run `ensureSession()` once and retry the request; on a second 401 surface a friendly "session error — reload" state.
- **429/503 handling:** reuse the existing retry-banner path — `postBriefing` already sets `err.retryable` for 503/504; extend it to treat 429 (budget) with its `Retry-After` as a soft, retryable "you're going a bit fast" banner rather than a dead-end.
- **Offline / demo unaffected:** offline live calls already fall back to the persisted `uwx.briefing.v1`; `ensureSession` failure offline just means no live fetch, same as today.
- **Service worker (`sw.js`):** ensure `/v1/session*` is on the network-only / never-cached path (POSTs already aren't cached, but add an explicit bypass so a challenge/mint is never served stale).

### 5.7 Adjacent hardening folded in (cheap, same PR family)

- **SA-12:** gate `/docs`, `/redoc`, `/openapi.json` behind `docs_enabled` (off in prod); trim `/v1/health` to a minimal liveness body and move the detailed `limits`/cache/active-mission fields behind an admin token or the private network; default the standalone `main()` bind to `127.0.0.1` (require an explicit flag for `0.0.0.0`).
- **SA-05 synergy:** because the token is an HttpOnly cookie, the SA-05 CDN-script risk cannot exfiltrate it — worth stating in the SA-05 remediation too.
- **SA-09 dependency:** the `Secure` cookie only ships over HTTPS. TLS must be live on the public host *before* `api_auth_enabled=1`. Track TLS-gate (SA-09) as a hard prerequisite for enabling the flag in production (see §7).

### 5.8 Optional: stdlib proof-of-work on mint (GA hardening, no third party)

The one weak point of anonymous sessions is that tokens are freely mintable. Per-IP mint limiting + per-IP-aggregate budgets already bound single-source abuse; PoW raises the cost of *distributed* mass minting without a CAPTCHA vendor (keeping "self-contained"):

- `/v1/session/challenge` → `{nonce, bits}`; `nonce` is an HMAC-signed value carrying an expiry so the server keeps no challenge table.
- Client finds `x` such that `sha256(nonce || x)` has `bits` leading zero bits (Web Worker).
- `/v1/session` verifies the solution before minting.

Ship it **behind `session_pow_enabled` (default off)**; enable at GA if telemetry shows minting abuse. Building it now (flagged off) means enabling it later is a config flip, not a code change.

## 6. Test plan

New hermetic tests (default suite stays green because `api_auth_enabled` defaults **off**; auth tests flip it via a settings/env fixture — `get_settings()` re-reads env each call, so monkeypatching works cleanly):

- `tests/test_api_auth.py`
  - **Acceptance #1:** with auth on, unauthenticated `POST` to each of the four endpoints → `401`.
  - **Acceptance #2:** mint a session, then each endpoint succeeds (using the offline `inputs` path so it's network-free).
  - **Acceptance #3:** a `TestClient` request (no nginx) without a cookie → `401` (proves in-app enforcement, not edge-only).
  - Token tamper (flip a byte) → 401; expired token → 401; token signed with a foreign secret → 401; rotation: a token signed with `session_secret_prev` still verifies.
  - Middleware coverage: a route that forgets the dependency is still gated by the allowlist.
- `tests/test_api_budget.py`
  - Per-principal breach → 429 + `Retry-After`; window refill restores access.
  - Per-IP aggregate: N tokens from one IP cannot exceed the IP ceiling (rotation defeated).
  - Global ceiling → 503 + `Retry-After` + a WARNING log.
  - Cache **hit** does not charge `cold` (budget unaffected by re-requesting the same mission).
  - **SA-03:** a principal at `budget_active_per_principal` registers no further missions; over-quota still briefs on demand.
  - Session mint rate limit → 429.
  - (If built) PoW: bad solution rejected, valid accepted.
- Update the existing API tests that will now need a session when auth is on to use a shared `authed_client` fixture; leave the default-off suite untouched.

## 7. Phased delivery

Each phase is independently shippable; auth stays invisible until the flag flips.

| Phase | Deliverable | Depends on | Effort |
|---|---|---|---|
| **0. Prereqs** | Generate + store `UPSTREAMWX_SESSION_SECRET` in the EnvironmentFile; confirm TLS live on public host (SA-09); confirm single-worker (done). | — | S |
| **1. Session core** | `auth.py` (mint/verify/cookie/dependency), `SessionMiddleware`, `POST /v1/session`, config keys, gate the four endpoints, frontend `ensureSession` + credentials/headers + 401 retry. Ships behind `api_auth_enabled` (off). | 0 | M |
| **2. Budgets** | `budget.py` (per-principal + per-IP + global), wire charges, SA-03 registration gating + per-principal quota, `/v1/health` trim + `/docs` off (SA-12), 429/503 frontend UX. | 1 | M |
| **3. Mint hardening** | Per-IP mint limiter; optional PoW module + `/v1/session/challenge` + Web Worker (flagged off). | 1 | S–M |
| **4. Deploy + rollout** | Env example + nginx strict mint zone; enable on **staging** with auth on; run acceptance tests **from outside** the trust boundary (incl. direct :8000 attempt); then flip prod. | 1–3, SA-09 | S |
| **5. Observability** | Admin-gated metrics (active principals, window fill, breach counts), log-based alerts for the global ceiling; document runbook. | 2 | S |

## 8. Deployment & rollout

- **EnvironmentFile** (`/etc/upstreamwx/upstreamwx.env`, template `deploy/upstreamwx.env.example`): add commented `UPSTREAMWX_API_AUTH_ENABLED=1`, `UPSTREAMWX_SESSION_SECRET=<openssl rand -hex 32>`, and the budget knobs (all with safe defaults documented as "current default"). Mode `0640 root:upstreamwx`, like the existing secrets there.
- **nginx** (`deploy/nginx/upstreamwx.conf`): keep the existing `limit_req` zones as defense-in-depth. Add a **strict** `limit_req` zone for `location = /v1/session` (mint is cheap to us but should be capped hard per IP). Cookies already pass through unchanged. No CORS added (keep single-origin).
- **Rollout order:** deploy code with the flag **off** (zero behavior change, fully reversible) → enable on staging → run acceptance + a small load test from an external network → enable in prod. **Rollback = flip `UPSTREAMWX_API_AUTH_ENABLED=0` and restart** (no code rollback needed).
- **Tailnet coexistence:** the auth layer is additive; the private beta can run with auth on *or* off behind the tailnet during the transition. The public host runs with it on.

## 9. Risks, edge cases, and interactions

- **Multi-worker / persistence (M0.1.1):** budget/mint counters are process-local, which is correct for the documented single-worker deployment. Signed tokens are stateless and already survive a restart or a future worker split; only the *counters* would need a shared store (Redis/SQLite) if the host ever scales out — the same "in-process now, persistent later" trajectory the briefing cache already documents. Note it in the module docstring so the boundary is explicit.
- **Secret rotation:** supported via `session_secret` + `session_secret_prev` (verify-only); rotate by promoting new→current, old→prev, then dropping prev after one TTL.
- **Mobile IP roaming / CGNAT:** budgets key on the live IP for the *aggregate* ceiling only; the *principal* is the token, so a roaming user isn't penalized for changing networks. Per-IP ceilings are set generously above the per-principal ones for this reason.
- **iOS standalone PWA cookies:** HttpOnly cookies work in `display: standalone`; verify in the acceptance pass (the audit's PWA/PDF/offline flow test already covers this surface).
- **Does NOT fix SA-02 or SA-04.** The per-principal `cold` budget *slows* the SA-02 cache-fill attack (an authenticated flood is throttled) but the real fix is input bounds + byte-aware cache eviction. SA-04 cross-user metadata leakage is orthogonal — sessions don't change the shared cache key. Both remain separate release blockers; this plan explicitly does not claim them.
- **Definition of done includes docs:** update CLAUDE.md "Milestone status" (a new access-gate paragraph) and add a `docs/changelog-<date>-sa01-auth.md` in the same change, per repo convention.

## 10. Summary

The public release replaces "URL = invitation" with an **app-issued anonymous principal** carried in an HttpOnly signed cookie, enforced **in the application** (so a direct :8000 hit is still denied), and used to attach **per-principal + per-IP + global** fair-use and cost budgets to every expensive or billable operation. Cache hits stay free, no personal data is collected, no third-party dependency is added, and the whole gate is a reversible config flag. It closes SA-01's three acceptance tests and delivers the "register only authorized principals" half of SA-03, while leaving SA-02 and SA-04 correctly scoped as their own work.
