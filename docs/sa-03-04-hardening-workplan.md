# SA-03 / SA-04 Remediation Workplan — Bound recurring scheduler work & isolate the cache key

- **Findings:**
  - **SA-03** — *Public requests create recurring scheduler workload* (High) — `docs/Security Audit 2026-07-14.md` §SA-03
  - **SA-04** — *Cache key collision leaks or substitutes mission metadata* (High) — §SA-04
- **Target:** the public beta server (Internet-exposed). Like the SA-02 plan, every control here must
  hold for an **unauthenticated Internet client** — it must not rely on the SA-01 access gate being
  active. Where the gate *is* active it adds a further per-principal bound (SA-01 already landed the
  "register only for authorized principals" half of SA-03); where it is *not* (dev, CLI, the tailnet
  beta) these controls still bound the work.
- **Constraint (explicit):** all hardening is **backend / server-side** (`src/upstreamwx/api/…`,
  `src/upstreamwx/config.py`). Nothing in `frontend/`. A scripted client that bypasses the PWA must be
  bounded entirely by the API.
- **Status:** ✅ **Implemented** (branch `claude/security-audit-sa-03-04-v4sc2s`). See §7 for the
  per-item done checklist and the changelog `docs/changelog-2026-07-15-sa-03-04.md`.

---

## 1. The two vulnerabilities, restated against the code

### SA-03 — one request becomes days of recurring background work

`BriefingService` registers **every** successful live, still-in-range briefing for scheduled refresh
(`service.py` `get_briefing` → `_register_active`), and the scheduler's `refresh_active` **re-ingests
every registered mission on every 6-hourly cycle** until its window ends. A mission may start up to 10
days out and span 7 days, so a single request can pin ~2 weeks of recurring ingest.

| Gap | Where (before this change) |
|---|---|
| Registry mutated from request threads **and** the scheduler thread with **no lock** | `service.py` `_active` dict; `refresh_active` iterates `list(self._active.items())` and `del`s while `get_briefing`/`_register_active` mutate |
| Refresh regenerates a mission **forever** (until window end) even if it was viewed **once** | `refresh_active` has no "recently viewed" gate — a fire-and-forget request keeps refreshing for days |
| A refresh pass has **no item / wall-clock / cost budget** | `refresh_active` loops the whole registry unconditionally |
| Scheduled generation **does not share** the request-generation concurrency cap | `refresh_active` calls `generate_briefing` directly, never acquiring `_gen_sem`; a heavy pass competes with interactive briefings |
| **No metrics** on registry size, pass duration, or work skipped | `refresh_active` returns only a bare regenerated count |

SA-01 already added: registration is gated to an authorized principal and capped at
`budget_active_per_principal` (3) **when the gate is active**. That is the "register only authorized
principals" recommendation. It does **not** cover the gate-off case, the unbounded-per-pass work, the
missing lock, the "refresh forever" lifetime, or metrics — which is what this plan adds.

### SA-04 — the cache key omits the mission metadata that reaches the response

`mission_cache_key` (`cache.py`) keys on activity / rounded location / window / phases / slot / radii /
optional inputs but **omits `name`, `party_size`, `route_note`**. The cached value is a
`GeneratedBriefing` whose `BriefingResult` embeds the **original** `Mission`, and the rendered Markdown
(`render.py:114`) and structured contract (`structured.py:440`) both print `mission.name`. So two users
at the same place / activity / window collide on one entry and the second is served the **first user's
mission name and presentation** — a cross-user disclosure and a cache-poisoning vector (pre-seed a
predictable location/window with a misleading label). `tests/test_api_cycles.py:86-95` currently
*asserts* this collision ("name is not part of identity").

Response-affecting audit of the metadata fields (verified by grep):
- `mission.name` → **rendered** (`render.py:114`, `structured.py:440`). The real leak.
- `party_size`, `route_note` → carried on `Mission`, **never rendered** and **never read by the engine**
  (`engine/` reads neither). Not a leak *today*, but user-supplied identity fields the audit flags.

---

## 2. Design decisions (and why)

### SA-04 — include the response-affecting metadata in the key (the audit's "immediate fix")

The audit offers two options: (1) *immediate* — include every response-affecting mission field in the
key; (2) *stronger* — cache shareable "conditions" under a metadata-free key and rebuild the
request-specific presentation for every response.

**We take option (1),** for these reasons:
- It **fully closes** all three SA-04 acceptance tests with minimal, self-contained risk (one function,
  one flipped test assertion) — no refactor of the deterministic generation core, so **NFR-4 and the
  golden renders are untouched**.
- SA-02 now **bounds** `name` (80), `route_note` (1000), `party_size` (1–200), so the only downside of
  option (1) — two identical-condition missions with different metadata no longer share a cache entry —
  is negligible: entries are small (byte-budget-bounded), cold work is already per-IP + per-principal
  cost-limited (SA-02/SA-01), and two *distinct* users briefing the **identical** lat/lon (≈11 m),
  minute-window, activity, and radii but a different name is a rare event even at scale (a single user
  reopening *their* mission keeps a stable name → still a cache hit → "reopening is free" is preserved).
- The stronger conditions/presentation split is genuinely better for cache **sharing**, but it is a
  larger, higher-risk refactor of `generate.py`/`render.py`/`structured.py` and the cache value type —
  disproportionate for a security remediation whose leak is a single rendered field. It is recorded as
  a **deferred enhancement** (§6) and it also unlocks the SA-03 domain-dedup (§rec 4).

We include **all three** metadata fields in the key (not just `name`): it satisfies the acceptance test
literally, and it is defensive — a future change that starts rendering `party_size`/`route_note` cannot
reintroduce a leak because those requests already key apart. Fields are folded in as a single
`repr((name, party_size, route_note))` part so distinct tuples always produce distinct keys (a raw
`|`-join of user strings could, in principle, be gamed into a collision).

**On removing `party_size`/`route_note` (the audit's secondary suggestion):** they are genuinely unused
in output and engine, so removing them would shrink both the leak surface (SA-04) and the retention
surface (SA-10). We **keep** them here (they are plumbed through the CLI `--party-size`/`--route-note`
and sent by the PWA planner, and removal is SA-10 privacy-minimization scope, not SA-04), but fold them
into the key so they are leak-safe now. Their removal is called out for SA-10.

### SA-03 — bound *which* missions refresh, *how much* each pass does, and *when*

We keep SA-01's per-principal registration cap and add the four missing controls, all backend and all
independent of the access gate:

1. **A lock around the registry** (rec 6, acceptance c). Every read/mutation of `_active` — registration,
   the "touch on view" below, eviction, pruning, the refresh snapshot — takes one `threading.Lock`. The
   slow `generate_briefing` work in a refresh pass runs **outside** the lock (snapshot under lock, gen
   outside, small re-check under lock), so a multi-minute pass never blocks request-thread registration.

2. **Refresh only recently-viewed missions** (rec 3, acceptance a — the biggest lever). Each registry
   entry carries a `last_seen` timestamp, set at registration and **bumped on every `get_briefing`**
   (cache hit *or* miss — a hit means the user reopened the app). A refresh pass **prunes and skips**
   any mission not viewed within `api_active_refresh_ttl_s` (default **12 h ≈ two cycles**). A refresh
   regeneration does **not** bump `last_seen`, so a fire-and-forget request stops refreshing after the
   TTL — one request becomes ≤ ~2 cycles of work, **not days**. An actively-planned mission (reopened
   periodically) stays warm.

3. **A hard per-pass budget** (rec 5, acceptance b). `refresh_active` stops cleanly when it has
   regenerated `api_refresh_pass_max_items` (default 64) **or** exceeded `api_refresh_pass_max_seconds`
   (default 240 s) wall-clock, whichever comes first; missions not reached this pass simply refresh next
   pass or on demand (NFR-6). Total work per pass is bounded regardless of registry size.

4. **Coordinate with request concurrency** (rec 6, acceptance b). Each refresh regeneration acquires the
   **same `_gen_sem`** the request path uses (so scheduled + interactive gens never exceed the
   concurrency cap and can't jointly OOM the host), but with a short `api_refresh_gen_wait_s` (default
   0.5 s) timeout: if a slot isn't free promptly the host is busy serving real users, so the pass
   **defers** the rest to the next cycle rather than competing. Refresh is strictly lower priority than
   interactive briefings — it uses spare capacity only.

5. **Metrics** (rec 7). `refresh_active` records a `RefreshStats` snapshot (registry size, regenerated,
   pruned-ended, pruned-stale, deferred, skipped-by-budget, duration). The scheduler logs it each cycle
   and `/v1/health` echoes the last pass's counts (non-secret — SA-12-safe) so a stuck or budget-bound
   scheduler is observable from a curl.

**On rec 2 ("global quota substantially below 256") and rec 4 (dedup by cycle/domain):** with the
recently-viewed TTL pruning idle missions and the per-pass budget capping work, the registry-count cap
(`api_active_missions_max`, 256) is now a **memory ceiling on the dict, not the work bound** — the work
is bounded by items 2–4 above. We keep the 256 default (lowering it would hurt legitimate multi-user
refresh on the public host without reducing the now-budgeted work) but document that operators may lower
it and that the substantive controls are the TTL + pass budget. Cross-mission **domain dedup** (rec 4)
requires the SA-04 conditions/presentation split and is deferred (§6); in the meantime the GEFS/REFS
grid cache (per cycle) and the watershed cache (per point) already dedupe the *expensive* sub-steps
across all missions in a pass, so the residual per-mission cost is the cheap engine+render over cached
grids — well within the pass budget.

### Invariants preserved

- **NFR-4 / deterministic engine.** No engine, threshold, or render change. `assess` and the golden
  SITREPs are untouched. SA-04 only changes a **cache key**; SA-03 only changes **when/whether** a
  briefing is regenerated, never its content.
- **NFR-6 graceful degradation.** Every new bound *reduces* work and fails toward "brief on demand
  later," never toward a crash or a stale-but-benign result. A deferred/pruned mission still briefs
  normally on the next request.
- **"Reopening the app is free."** Cache hits are still free (no gen, no budget charge). SA-04 keeps a
  stable-metadata reopen a hit; SA-03's `last_seen` bump on a hit is an O(1) locked dict touch.

---

## 3. Workstreams

### WS-1 — SA-04: fold the mission metadata into the cache key

**File:** `src/upstreamwx/api/cache.py` (`mission_cache_key`).

Append one unambiguous metadata part:

```python
# SA-04: fold in the user-supplied mission metadata that reaches the response. `name` is
# rendered (render.py, structured.py); party_size/route_note are carried for future use.
# Without this, two requests differing only in these fields collide on one entry and the
# second is served the first's mission name/presentation (cross-user leak / cache poisoning).
# repr() of the tuple is collision-safe where a raw join of user strings would not be.
parts.append(f"meta={(mission.name, mission.party_size, mission.route_note)!r}")
```

Update the docstring to state that metadata is now part of identity.

**Tests** (`tests/test_api_cycles.py`, `tests/test_api_cache_isolation.py` new):
- **Flip** `test_mission_key_stability_and_sensitivity`: a renamed mission now has a **different** key
  (replaces the "name is not part of identity" assertion — acceptance test 3). Add party_size/route_note
  sensitivity and confirm location still matters.
- **No-leak (service level, acceptance test 1):** brief mission A (`name="Secret Slot"`), then brief an
  identical-conditions mission B (`name="mission"`); assert B's response `mission.name == "mission"` and
  its markdown does not contain "Secret Slot". A conditions-only change still hits; a metadata change
  misses and renders the current request (acceptance test 2).

### WS-2 — SA-03: lock the active registry

**File:** `src/upstreamwx/api/service.py`.

- Add `self._active_lock = threading.Lock()`.
- Guard `_register_active`, the new `_touch_active`, eviction, `active_count`, and the refresh
  snapshot/prune under it. **Never** hold it across `generate_briefing` or a `cache.put`.

### WS-3 — SA-03: recently-viewed lifetime

**Files:** `service.py`, `config.py`.

- `_Registered` gains `last_seen: datetime`.
- `_register_active(..., now)` sets `last_seen=now`.
- New `_touch_active(key, now)` bumps `last_seen` for an existing entry (no-op if absent); called from
  `get_briefing` on **both** the hit and the cold-miss paths.
- `refresh_active` prunes/skips entries with `now - last_seen > api_active_refresh_ttl_s`.

`config.py`:
```python
# How long since it was last VIEWED a mission stays eligible for scheduled refresh (SA-03). A
# refresh regeneration does NOT count as a view, so a fire-and-forget request stops refreshing
# after this window — one request becomes at most ~2 cycles of work, not days. A reopened
# (actively planned) mission stays warm. Default 12 h ≈ two 6-hourly cycles.
api_active_refresh_ttl_s: float = 12 * 3600.0
```

### WS-4 — SA-03: per-pass item + wall-clock budget

**Files:** `service.py` (`refresh_active`), `config.py`.

`refresh_active` stops cleanly at `api_refresh_pass_max_items` regenerations or
`api_refresh_pass_max_seconds` wall-clock; the remainder is counted as `skipped_budget` and refreshes
next pass. Return type stays `int` (regenerated count — keeps existing tests' `== 1` / `== 0`); detailed
counts go on `self._last_refresh_stats`.

```python
# Hard per-pass caps for the scheduled refresh (SA-03) so one pass can't run unbounded work.
# Missions not reached this pass simply refresh next cycle or on demand (NFR-6).
api_refresh_pass_max_items: int = 64        # max regenerations per pass (0 = unlimited)
api_refresh_pass_max_seconds: float = 240.0 # wall-clock budget per pass  (0 = unlimited)
# How long a refresh regeneration waits for a shared generation slot before yielding the rest
# of the pass to interactive briefings (SA-03). Short → refresh uses spare capacity only.
api_refresh_gen_wait_s: float = 0.5
```

### WS-5 — SA-03: share the generation concurrency cap

**File:** `service.py`.

Each refresh regeneration acquires `_gen_sem` with `timeout=api_refresh_gen_wait_s`; on timeout the pass
records `deferred` and stops (host busy with interactive work). No-op coordination when `_gen_sem` is
disabled (`briefing_max_concurrency <= 0`), but the item/time budget still applies.

### WS-6 — SA-03: per-mission resilience + metrics + health

**Files:** `service.py`, `scheduler.py`, `app.py`.

- Each refresh regeneration is wrapped in a per-mission `try/except` so one bad mission is counted
  (`RefreshStats.failed`) and logged, and the pass continues instead of aborting (NFR-6). The item
  budget counts attempts (successes + failures) so a high-failure pass still stops at the cap.
- `RefreshStats` frozen dataclass (incl. `failed`); `service.last_refresh_stats` property, recorded
  **always** (even after failures) so the surfaced stats are never stale; scheduler logs it each pass.
- `/v1/health` gains a compact `refresh` block (last-pass counts + registry size) and the new limits echo
  the TTL / pass caps. Counts only — SA-12-safe.

---

## 4. New configuration (public-beta values)

| Setting | Default | Purpose |
|---|---|---|
| `api_active_refresh_ttl_s` | `43200` (12 h) | WS-3 — refresh only recently-viewed missions |
| `api_refresh_pass_max_items` | `64` | WS-4 — per-pass regeneration cap |
| `api_refresh_pass_max_seconds` | `240` | WS-4 — per-pass wall-clock budget |
| `api_refresh_gen_wait_s` | `0.5` | WS-5 — yield refresh to interactive work |

All read via the `UPSTREAMWX_` prefix; documented in `deploy/upstreamwx.env.example`. `/v1/health.limits`
echoes them. SA-04 adds **no** config (a key change only).

---

## 5. Test plan → acceptance criteria

**SA-04** (`tests/test_api_cache_isolation.py`, `tests/test_api_cycles.py`):
1. *Requests differing only in name/party_size/route_note never return each other's metadata* → distinct
   keys + the service no-leak test above.
2. *A conditions-cache hit still produces presentation from the current request* → a metadata-only change
   misses and renders the current name; a conditions-only re-request hits.
3. *Replace the "name is not part of identity" assertion* → flipped in `test_api_cycles.py`.

**SA-03** (`tests/test_api_scheduler_budget.py` new; existing `test_api_cycles.py` stays green):
- *Creating many missions cannot cause unbounded/multi-day work* → a mission not re-viewed past
  `api_active_refresh_ttl_s` is pruned and not refreshed; a pass over N ≫ budget missions regenerates
  ≤ `api_refresh_pass_max_items`.
- *A pass stops within budget and does not starve interactive briefings* → with `_gen_sem` pre-acquired
  (host "busy"), a pass defers cleanly and returns promptly without blocking; item/time caps hold.
- *Concurrency* → threads registering / touching / evicting / refreshing simultaneously raise nothing and
  leave a consistent registry (the lock).
- Existing `test_scheduler_refreshes_active_in_range_missions` / `_drops_ended_missions` stay green (TTL
  default 12 h > their 4 h delta; ended-window prune unchanged).

Regression guards: golden SITREPs, the validation corpus, and the sample-contract round-trip stay green
(engine + render unchanged). `ruff check .` clean (line length 100).

---

## 6. Deferred (recorded, not done here)

- **Conditions/presentation cache split** (SA-04 "stronger design"): cache the shareable ingest+engine
  result under a metadata-free key, rebuild `MissionView`+Markdown per request. Restores cross-metadata
  cache sharing and structurally prevents any presentation-object reuse. Larger refactor of
  `generate.py`/`render.py`/`structured.py`; also unlocks ↓.
- **Cross-mission domain dedup in a refresh pass** (SA-03 rec 4): ingest once per conditions group, render
  each mission's presentation from the shared result. Depends on the split above.
- **Remove `party_size`/`route_note`** if they stay unused (SA-10 privacy minimization).
- **Shared-store counters/registry** for a multi-worker host (the M0.1.1 upgrade the cache already
  documents). In-process is correct for the single-worker deployment.

---

## 7. Definition of done

- [ ] SA-04: `mission_cache_key` folds in name/party_size/route_note; the "name is not part of identity"
      assertion is replaced; a service-level no-leak test passes (WS-1).
- [ ] SA-03: `_active` mutation is lock-guarded; concurrency test passes (WS-2).
- [ ] SA-03: refresh prunes/skips missions not viewed within `api_active_refresh_ttl_s`; a hit and a miss
      both bump `last_seen` (WS-3).
- [ ] SA-03: a pass stops at the item/time budget; the remainder is counted and refreshes later (WS-4).
- [ ] SA-03: refresh shares `_gen_sem` and yields to interactive work on contention (WS-5).
- [ ] SA-03: `RefreshStats` logged each pass; `/v1/health` echoes last-pass counts + new limits (WS-6).
- [ ] Existing scheduler/cycle tests stay green; corpus + golden renders + sample-contract green; `ruff`
      clean.
- [ ] `deploy/upstreamwx.env.example`, `CLAUDE.md` milestone status, and
      `docs/changelog-2026-07-15-sa-03-04.md` updated.

All changes live under `src/upstreamwx/api/` and `src/upstreamwx/config.py` (plus tests and the deploy
env) — **backend only**, as required.
