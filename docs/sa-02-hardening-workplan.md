# SA-02 Remediation Workplan — Bound mission input & count-only caches

- **Finding:** SA-02 — *Unbounded mission input can exhaust memory through count-only caches* (High)
- **Source:** `docs/Security Audit 2026-07-14.md` §SA-02
- **Target:** public beta server (Internet-exposed). The private beta is already gated by a
  tailnet (SA-01), so this plan does **not** rely on any access gate — every control here must
  hold for an unauthenticated Internet client.
- **Constraint (explicit):** all hardening is **backend / server-side** (`src/upstreamwx/api/…`,
  `src/upstreamwx/config.py`). The PWA's own client-side validation is defence-in-depth only and
  is out of scope; a modified or scripted client that bypasses `frontend/js/app.js` must be
  bounded entirely by the API.
- **Deliverable of this document:** an implementation-ready workplan. No product code is changed
  by this file.

---

## 1. The vulnerability, restated against the code

The mission-planning request model accepts unbounded input, and the in-process caches that
retain the result are bounded by **entry count only**:

| Gap | Where (current code) |
|---|---|
| `name` has no length cap | `api/models.py:90` (`name: str = "mission"`) |
| `party_size`, `route_note` unbounded | `api/models.py:93-94` |
| `inputs` is an untyped `dict` expanded with `HazardInputs(**data)` | `api/models.py:115-118, 182-188` — unknown keys / non-finite floats become 500s, not clean 422s |
| No app-level request-byte cap on `/v1/briefing` | edge-only `client_max_body_size 4m` at `deploy/nginx/upstreamwx.conf:38-39`; the standalone uvicorn entry point has none |
| Caches bounded by count, not bytes | `api/cache.py:38-72` (`BoundedLRU`), default 512 entries (`config.py:118`); same for `service._result_store` (`service.py:88`) |
| `inputs` entries never expire | `STATIC_TOKEN` never invalidates (`api/cache.py:118`; `service.py:146`) |
| `/v1/briefing` cold generation is not cost-rate-limited per principal | only nginx 2 r/s + the `_gen_sem` concurrency cap (`service.py:76-78`); no per-IP miss budget |

**Exploit (from the audit):** a ~3 MiB `name` + `inputs: {}`, varying a cache-key field (e.g.
rounded `lat`) across 512 requests, retains multiple GiB behind the "512 entry" cap. The
explicit-inputs path skips live ingest and never expires, making it cheap and durable until
process restart. At the repo's 2 r/s edge rate, 512 entries fill in ~4.3 min from one address.

**Fix strategy:** make an oversized or malformed request *un-representable* at the model boundary,
cap the bytes any request can spend, cap the bytes the caches can retain (with a TTL for the
static path), disable the never-expiring replay path in production, and charge cold generations
against a per-IP cost budget. Every one of these is server-side.

### Invariants this plan must not break

- **NFR-4 / deterministic engine.** The strict `inputs` model must produce a `HazardInputs`
  dataclass **bit-identical** to today's `HazardInputs(**data)`, so offline replays and the
  validation corpus reproduce exactly. Engine, thresholds, and golden renders are untouched.
- **FR-25 offline parity.** The CLI/corpus replay path (`--inputs`) keeps working; only the
  **public API's** acceptance of the replay path is feature-flagged (WS-3).
- **NFR-6 graceful degradation.** New limits reject *before* work is spent and return bounded
  4xx/503, never a crash or a silent benign result.

### Out of scope (tracked separately, do not fold in here)

- **SA-01** — access gate (handled for private beta by the tailnet).
- **SA-04** — cache-key omits `name`/`party_size`/`route_note` (cross-user metadata leak). This
  plan **shrinks its blast radius** (an 80-char name is a far smaller poisoning payload than 3 MiB)
  but does **not** fix the collision. Keep `tests/test_api_cycles.py:86-95`
  (`name is not part of identity`) unchanged; it flips under SA-04, not SA-02.
- **SA-03** — anonymous requests creating recurring scheduler work. WS-6's miss-budget reduces the
  rate of new `_register_active` entries as a side effect, but the registry-authorization fix is SA-03.

---

## 2. Workstreams

Six workstreams, one per audit recommendation bullet. Each lists the change, exact touch points,
new config, and tests. WS-1/WS-2/WS-3 are the load-bearing input bounds; WS-4/WS-5/WS-6 are the
retained-cost bounds.

### WS-1 — Bound every `MissionSpec` string and collection

**File:** `src/upstreamwx/api/models.py` (`class MissionSpec`).

Add server-side constraints (audit's suggested starting points):

```python
name: str = Field(default="mission", max_length=80)
party_size: int | None = Field(default=None, ge=1, le=200)
route_note: str | None = Field(default=None, max_length=1000)
```

Notes:
- Keep `name`'s default so existing callers are unaffected; the cap is the only new behaviour.
- `name` (80) is deliberately ≤ the response-side `MissionView.name` cap (200, `models.py:226`),
  so a value that validates on the way in always renders on the way out.
- Consider `str_strip_whitespace=True` on `MissionSpec.model_config` so a padded 80-char name
  can't smuggle extra bytes; low-risk, optional.
- These are pure Pydantic `Field` constraints → automatic **422 before** `to_mission()` /
  `get_briefing()` runs. No generation cost is spent (acceptance test 1).

**Tests** (`tests/test_api_limits.py`): parametrized `MissionSpec` construction with
`name="x"*81`, `route_note="x"*1001`, `party_size=0`, `party_size=201` each raise
`ValidationError`; the boundary values (80 / 1000 / 1 / 200) pass.

---

### WS-2 — Replace `inputs: dict` with a strict Pydantic `HazardInputsSpec`

**File:** `src/upstreamwx/api/models.py`.

Today `inputs: dict | None` (`models.py:115`) is expanded by `to_inputs()` via
`HazardInputs(**data)` (`models.py:188`). Unknown keys raise `TypeError` (→ 500), and NaN/inf/
out-of-range values pass straight through to the engine.

Introduce a Pydantic mirror of the `HazardInputs` dataclass (`engine/models.py:102-161`) that
rejects unknown fields, non-finite floats, and out-of-range values, then converts to the
dataclass unchanged.

```python
# Reusable constrained scalar types
Prob    = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]  # percent [0,100]
Unit    = Annotated[float, Field(ge=0, le=1,  allow_inf_nan=False)]   # member support [0,1]
FiniteF = Annotated[float, Field(allow_inf_nan=False)]                # temps, rates, CAPE, wind

class HazardInputsSpec(BaseModel):
    """Strict request representation of engine HazardInputs (SA-02).

    extra='forbid' -> unknown keys are 422 (not a 500 from HazardInputs(**data)); every
    float rejects NaN/inf; probabilities are clamped to their real domains. to_dataclass()
    reproduces the exact engine dataclass so replays stay bit-identical (NFR-4, FR-25).
    """
    model_config = ConfigDict(extra="forbid")

    # NWS product booleans (defaults mirror the dataclass exactly)
    flash_flood_warning: bool = False
    # ... every boolean field, same defaults as engine/models.py ...
    nws_products_available: bool = True

    gefs_p_precip: Prob | None = None
    gefs_p_tstm: Prob | None = None
    measurable_precip: bool | None = False          # tri-state preserved
    convective_rate_in_per_hr: FiniteF | None = None
    cape_jkg: FiniteF | None = None
    refs_p_precip: Prob | None = None
    refs_p_lightning: Prob | None = None

    member_support: dict[str, Unit] = Field(default_factory=dict, max_length=8)
    source_agreement: str = Field(default="consistent", max_length=16)
    spc_category: str | None = Field(default=None, max_length=32)
    afd_storm_mode: str | None = Field(default=None, max_length=32)
    afd_flood_mention: bool = False

    heat_index_f: FiniteF | None = None
    apparent_temp_f: FiniteF | None = None
    wind_mph: FiniteF | None = None
    antecedent_precip_24_72h: bool | None = False   # tri-state preserved
    domain_complete: bool = True
    dry_party: bool = False

    @field_validator("member_support")
    @classmethod
    def _known_hazards(cls, v: dict[str, float]) -> dict[str, float]:
        allowed = {h.value for h in Hazard}
        bad = set(v) - allowed
        if bad:
            raise ValueError(f"unknown hazard keys in member_support: {sorted(bad)}")
        return v

    def to_dataclass(self) -> HazardInputs:
        return HazardInputs(**self.model_dump())
```

Wire it into `MissionSpec`:

```python
inputs: HazardInputsSpec | None = None

@field_validator("inputs", mode="before")
@classmethod
def _unwrap_envelope(cls, v):
    # Preserve the corpus/CLI {"inputs": {...}} envelope the old to_inputs() accepted
    # (models.py:186) before strict validation of the inner object.
    if isinstance(v, dict) and set(v) == {"inputs"} and isinstance(v["inputs"], dict):
        return v["inputs"]
    return v

def to_inputs(self) -> HazardInputs | None:
    return self.inputs.to_dataclass() if self.inputs is not None else None
```

Consequences:
- Unknown keys, NaN/inf, and out-of-range probabilities now fail at the FastAPI model boundary →
  bounded **422** (acceptance test 2), not a 500 or an engine input.
- `to_inputs()` still returns the same dataclass; `mission_cache_key`'s
  `repr(sorted(vars(inputs).items()))` (`cache.py:97`) is unaffected.
- `frame_stream` (`app.py:310`) calls `spec.to_inputs()` — unchanged signature, still works.

**Determinism guard (critical test):** the pydantic spec's field set and defaults must stay in
lockstep with the dataclass. Add:

```python
def test_hazardinputs_spec_matches_dataclass():
    import dataclasses
    dc = {f.name for f in dataclasses.fields(HazardInputs)}
    assert set(HazardInputsSpec.model_fields) == dc          # no drift
    # every corpus/fixture inputs vector reproduces the exact dataclass
    for vec in _all_corpus_inputs():
        assert HazardInputsSpec(**vec).to_dataclass() == HazardInputs(**vec)
```

Drive the second assertion off `tests/corpus/*.yaml` and
`tests/fixtures/sitrep/sample_inputs.yaml` so any future field addition that isn't mirrored
fails CI.

---

### WS-3 — Feature-flag the replay/`inputs` path in production

Ordinary PWA users never send `inputs`; it is the FR-25 dev/corpus/CLI path. It is also the
*durable* half of the exploit (never-expiring static entries, no ingest cost). Disable it on the
public server.

**File:** `src/upstreamwx/config.py`

```python
# Accept the offline HazardInputs replay path on the public API (FR-25). Ordinary PWA users
# never send `inputs`; it is a dev/corpus/CLI affordance and creates non-expiring static cache
# entries (SA-02). Default on for CLI/dev parity; set UPSTREAMWX_API_ALLOW_INPUTS_REPLAY=0 on
# the public beta so an anonymous client cannot pin cheap, durable cache entries.
api_allow_inputs_replay: bool = True
```

**File:** `src/upstreamwx/api/service.py` — gate centrally in `get_briefing` (and mirror in the
frame path) before any work:

```python
if spec.inputs is not None and not get_settings().api_allow_inputs_replay:
    raise InputsReplayDisabled()   # -> mapped to 403 in app.py
```

Add a small exception + a 403 mapping in `app.py`'s `briefing` and `frame_stream` handlers,
alongside the existing `MissionWindowError`/`BriefingBusy` mapping (`app.py:279-286`).

Deployment: the public-beta `deploy/config.env` sets `UPSTREAMWX_API_ALLOW_INPUTS_REPLAY=0`.
Dev, CLI, and the offline test suite leave it default-on, so `--inputs` and the corpus keep
working (FR-25 preserved where it matters).

---

### WS-4 — Application-level request-byte limit (streaming, not just nginx)

The audit wants the app itself to reject oversized bodies (the standalone entry point,
`app.py:494`, has no edge in front; nginx config can also drift). A legitimate `MissionSpec` —
even with a full `inputs` vector — is a few KB.

**File:** `src/upstreamwx/api/app.py` — add a pure-ASGI body-size middleware scoped to the JSON
mission endpoints:

```python
class MaxBodySizeMiddleware:
    """Reject request bodies over a byte cap before the handler parses them (SA-02).

    Enforced two ways: a cheap Content-Length precheck (present on normal requests) and a
    streaming byte count over the wrapped receive channel (authoritative for chunked uploads,
    which omit Content-Length). Applies only to the small JSON mission endpoints; the PDF
    endpoint keeps its own larger 2 MiB cap (app.py:60, 367-371).
    """
```

- Cap: `api_max_request_bytes` (config) applied to `POST /v1/briefing`, `/v1/briefing/frame`,
  `/v1/watershed/warm`. Default **65_536** (64 KiB) — ~orders of magnitude above a real request,
  ~orders of magnitude below the exploit.
- Content-Length over cap → **413** immediately. Streaming count over cap → 413 (abort the
  receive). Mirror the pattern the PDF handler already uses (`app.py:364-371`) but as reusable
  middleware so `/v1/briefing` gets it without reading the body by hand.
- Keep the PDF endpoint's existing `_PDF_MAX_BODY_BYTES` = 2 MiB path (larger, legitimate).

**Config** (`config.py`):
```python
# App-level request-byte cap for the JSON mission endpoints (SA-02). A real MissionSpec is a
# few KB even with a full inputs vector; anything larger is an abuse payload. Enforced in-app
# (not just nginx client_max_body_size) so the standalone entry point and a drifted edge config
# are both covered. The PDF endpoint keeps its own 2 MiB cap.
api_max_request_bytes: int = 64 * 1024
```

**Edge, defence-in-depth (not the load-bearing fix):** tighten
`deploy/nginx/upstreamwx.conf` so `location /v1/briefing` and `/v1/watershed/warm` carry
`client_max_body_size 64k;`, leaving the 2m+ allowance only on `/v1/briefing/pdf`. Flagged as
complementary — the plan does not depend on it.

**Tests:** `TestClient` POST to `/v1/briefing` with a 128 KiB body → 413 and the service's
generate is never entered (assert `service.cache` length unchanged / patch `generate_briefing`
to raise if called).

---

### WS-5 — Byte-aware cache bounding + TTL for static entries

**File:** `src/upstreamwx/api/cache.py`.

Extend `BoundedLRU` (or add a `SizedLRU`) with a **byte budget** alongside the count cap, and a
per-entry insertion timestamp for TTL:

- `put(key, value, *, size_bytes, now)` records `size_bytes`; eviction loops while
  `len > maxsize` **or** `total_bytes > max_bytes`, popping LRU.
- `get(key, *, now)` treats an entry older than an optional `ttl_s` as a miss and evicts it
  (covers the static-replay durability even if WS-3 is somehow off).
- `BriefingCache.put` estimates retained bytes cheaply and passes them down. A `GeneratedBriefing`
  is dominated by its `markdown` string plus the structured payload and the retained `Mission`
  (which holds `name`/`route_note`). A robust, cheap estimate:

```python
def _estimate_bytes(b: GeneratedBriefing) -> int:
    n = len(b.markdown.encode("utf-8"))
    m = b.result.mission
    n += len((m.name or "")) + len((m.route_note or ""))
    return n + 2048  # fixed overhead for the structured/result graph
```

  (Because WS-1/WS-2 now bound `name`, `route_note`, and every `inputs` value, each entry is
  already small; the byte budget is the belt-and-suspenders that makes acceptance test 3 hold
  regardless.)

- Apply the same byte budget to `service._result_store` (`service.py:88`), estimating from the
  retained `Mission` strings + a constant.

Time source: the module already uses no wall clock in the cache; inject `now` (monotonic) the way
`_TokenBucketLimiter` does (`app.py:111`) so tests are deterministic. Do **not** call
`time.monotonic()` implicitly in a way that breaks reproducibility of `mission_cache_key`
(the key is unchanged).

**Config** (`config.py`):
```python
# Resident byte budget for the briefing + result caches (SA-02). Count caps alone don't bound
# memory: one large entry × N can retain GiB. Evict LRU until BOTH the entry cap and this byte
# budget hold. With mission fields and inputs now bounded (WS-1/WS-2) entries are small; this
# guarantees the cap even under a max-legal-size load test.
api_cache_max_bytes: int = 256 * 1024 * 1024
# TTL for deterministic static (inputs-replay) cache entries (SA-02). They previously never
# expired (cache.py STATIC_TOKEN); bound their lifetime so a pinned replay entry cannot persist
# for the whole process lifetime. Belt-and-suspenders behind api_allow_inputs_replay=0 (WS-3).
api_static_entry_ttl_s: float = 3600.0
```

Thread `api_cache_max_bytes` and `api_static_entry_ttl_s` through `BriefingService.__init__`
(`service.py:70-73`) next to the existing `api_cache_max_entries` read.

**Tests:** insert N entries whose estimated bytes exceed `api_cache_max_bytes` → assert
`len(cache)` and summed bytes stay under budget (oldest evicted). A static entry `get` past
`ttl_s` (injected `now`) returns None.

---

### WS-6 — Rate-limit cache **misses** (cost), not just request count

`/v1/briefing` today has no per-IP limiter in the app (`app.py:264-287` deliberately omits it);
it relies on nginx 2 r/s + `_gen_sem` concurrency. That bounds *concurrency*, not *total cold
work per principal* — the audit's "rate-limit cache misses and request cost."

Split cache lookup from generation so the miss limiter gates only cold work, and cache **hits
stay free** (the existing design goal, `app.py:74-80`):

1. Add a per-IP token bucket `_briefing_miss_limiter` (reuse the existing `_TokenBucketLimiter`,
   `app.py:93-131`), budget `api_briefing_miss_rate_per_min` (config, default ~10/min).
2. Refactor `BriefingService.get_briefing` (`service.py:130-179`) into:
   - `lookup(spec, *, now) -> BriefingResponse | None` — computes key+token, returns a cached
     response or `None` (no generation, no limiter);
   - `generate(spec, *, now) -> BriefingResponse` — the cold path (the current body from the
     `_gen_sem.acquire()` block onward).
3. In the `briefing` endpoint: call `lookup`; on hit return it (free). On miss, `_enforce_rate_limit(_briefing_miss_limiter, request)` → 429 + Retry-After when over budget, **then** `generate`.

This keeps the reopen-the-app / scheduled-refresh hit path zero-cost while capping how much cold
ingest one IP can force. It composes with `_gen_sem` (concurrency) and nginx (edge) as layered
defence, and — as a side benefit — throttles the rate at which one IP can seed `_register_active`
(partial pressure relief for SA-03, not a fix).

**Config** (`config.py`):
```python
# Per-IP budget for cold /v1/briefing generations (cache MISSES), SA-02. Cache hits are free and
# uncounted; only work that spends live ingest is charged. Complements the nginx edge limit and
# the _gen_sem concurrency cap with a per-principal cost budget. ~10/min is generous for a real
# planning session (each distinct mission is one miss) yet caps abuse. 0 disables (load tests).
api_briefing_miss_rate_per_min: float = 10.0
```

Gate the new limiter under the existing `api_rate_limits_enabled` switch (`config.py:137`,
`app.py:161`) so load tests can turn it off.

**Tests:** with the miss limiter enabled and `generate` patched to a stub, N+1 distinct-mission
POSTs from one IP → the (N+1)th returns 429 + Retry-After; a repeated identical mission (cache
hit) is never charged (issue 100 hits, assert 200s).

---

## 3. New configuration (public-beta values)

| Setting | Default (dev/CLI) | Public beta | Purpose |
|---|---|---|---|
| `api_allow_inputs_replay` | `True` | **`0`** | WS-3 — disable never-expiring replay path |
| `api_max_request_bytes` | `65536` | `65536` | WS-4 — app-level body cap |
| `api_cache_max_bytes` | `268435456` | tune to host RAM | WS-5 — byte budget for caches |
| `api_static_entry_ttl_s` | `3600` | `3600` | WS-5 — TTL for static entries |
| `api_briefing_miss_rate_per_min` | `10` | `10` (tune) | WS-6 — per-IP cold-miss budget |

All read via the existing `pydantic-settings` `UPSTREAMWX_` prefix (`config.py:21-26`); the
public-beta values go in the git-ignored `deploy/config.env` for that environment. `/v1/health`'s
`limits` block (`app.py:250-260`) should echo the new caps so "what is this box configured to do"
stays a one-curl check (mind SA-12 — keep it to non-secret counts, which these are).

---

## 4. Test plan → acceptance criteria

The audit's three acceptance tests, mapped to concrete offline tests (all hermetic, no network,
no LLM — extend `tests/test_api_limits.py` and `tests/test_api_models.py`):

1. **"Oversized fields return 422 or 413 without invoking generation."**
   - WS-1: `name`/`route_note`/`party_size` over cap → 422 at model construction.
   - WS-4: 128 KiB body → 413; patch `generate_briefing` to raise-if-called and assert it isn't.
2. **"Unknown `inputs` keys and non-finite values return a bounded 422 response."**
   - WS-2: `inputs={"bogus": 1}` → 422 (`extra='forbid'`); `inputs={"gefs_p_precip": "inf"}`
     and `NaN` → 422 (`allow_inf_nan=False`); `inputs={"gefs_p_precip": 150}` → 422 (range).
   - Determinism guard: field-set/defaults match the dataclass; every corpus vector round-trips
     to an identical `HazardInputs`.
3. **"A load test using the maximum legal request cannot exceed the configured cache memory budget."**
   - WS-5: fill the cache with max-legal entries (80-char name, 1000-char route_note, full
     inputs) beyond `api_cache_max_bytes` → assert summed estimated bytes and `len(cache)` stay
     under budget; oldest evicted.

Plus regression guards: keep the golden SITREP renders (`tests/test_sitrep_render.py`) and the
corpus (`tests/test_engine_corpus.py`) green — engine output is unchanged. Keep
`tests/test_api_models.py`'s sample-contract round-trip green (response models untouched).
Run `ruff check .` (line length 100) before finishing.

---

## 5. Sequencing, effort, and risk

**Order** (independent-first; each lands green on its own):

1. WS-1 (field bounds) — smallest, highest value, zero coupling. ~0.5 day.
2. WS-2 (strict `HazardInputsSpec` + determinism test) — the substantive one; the parity test is
   the guardrail. ~1–1.5 days.
3. WS-3 (replay feature flag + 403 mapping) — small, config + one gate. ~0.5 day.
4. WS-5 (byte budget + TTL in cache) — self-contained in `cache.py`/`service.py`. ~1 day.
5. WS-4 (ASGI body middleware) — self-contained; test with `TestClient`. ~0.5–1 day.
6. WS-6 (miss limiter + `lookup`/`generate` split) — the only refactor of `service.get_briefing`;
   do last so it rebases over the others cleanly. ~1 day.

**Total:** ~4.5–5.5 engineer-days including tests and the deploy-config change.

**Risks & mitigations:**

- *`HazardInputsSpec` drifting from the dataclass* → the field-set assertion + corpus round-trip
  test fail CI on any un-mirrored field addition (WS-2). This is the single biggest correctness
  risk and it is test-enforced.
- *Breaking FR-25 offline replays* → WS-3 defaults the flag **on** for dev/CLI; only the public
  deployment turns it off. The corpus/CLI never go through the public API model.
- *Byte-estimate inaccuracy* → the estimate is intentionally an over-approximation (fixed
  overhead added); with WS-1/WS-2 bounding the inputs, entries are small, so the budget is a
  ceiling, not a tight fit. Tune `api_cache_max_bytes` to the host.
- *Miss-limiter false positives for a heavy legitimate planner* → 10 distinct cold missions/min
  is well above a real session (most re-fetches are cache hits and free); the budget is tunable
  and disable-able via `api_rate_limits_enabled`.
- *Middleware ordering* → the body cap must run before FastAPI parses the typed body; register it
  as outermost so it sees the raw receive channel.

---

## 6. Definition of done

- [ ] `MissionSpec.name`/`party_size`/`route_note` bounded; over-cap → 422 (WS-1).
- [ ] `MissionSpec.inputs` is `HazardInputsSpec` (`extra='forbid'`, finite floats, ranged
      probabilities); unknown key / NaN / inf / out-of-range → 422; `to_inputs()` yields an
      identical `HazardInputs` for every corpus vector (WS-2).
- [ ] `api_allow_inputs_replay=0` makes the public API reject `inputs` (403) while CLI/dev/tests
      keep FR-25 (WS-3).
- [ ] Bodies over `api_max_request_bytes` on the mission endpoints → 413 in-app, generation never
      entered; standalone entry point covered too (WS-4).
- [ ] Briefing + result caches evict on a byte budget; static entries carry a TTL (WS-5).
- [ ] Cold `/v1/briefing` misses are per-IP cost-limited (429 + Retry-After); cache hits stay
      free (WS-6).
- [ ] `/v1/health.limits` echoes the new caps.
- [ ] The three audit acceptance tests pass; corpus + golden renders + sample-contract tests stay
      green; `ruff check .` clean.
- [ ] Public-beta `deploy/config.env` carries the hardened values (§3).

All changes live under `src/upstreamwx/api/` and `src/upstreamwx/config.py` (plus tests and the
deploy env) — **backend only**, as required.
