"""Runtime configuration, loaded from environment / ``.env``.

No secrets are required for M0.0; this establishes the loading pattern (PRD §7,
secrets handling) so later milestones (e.g. the M0.2 Haiku framing key) drop in
without rework.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration sourced from env vars and an optional ``.env`` file."""

    model_config = SettingsConfigDict(
        env_prefix="UPSTREAMWX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Root for runtime caches (raw GRIB2 pulls, WBD downloads). Git-ignored.
    data_dir: Path = Path("./data")

    # Static PWA directory served single-origin by the API (M0.4). None -> the repo's
    # ``frontend/`` (resolved relative to the package); set UPSTREAMWX_FRONTEND_DIR to
    # override for a packaged deployment, or to "" to disable static serving entirely.
    frontend_dir: Path | None = None

    # Optional override for the GEFS source base URL (default: NOMADS gens/prod, the operational
    # GEFS endpoint; SREF replacement). Rarely needed — GEFS is already on its production path.
    gefs_base_url: str | None = None

    # REFS source feed (HREF replacement, NWS SCN 26-48). Selects the (base URL, ensemble-product
    # subdir) pair the REFS provider reads:
    #   "aws"         -> noaa-rrfs-pds/rrfs_a + "enspost"  (RRFS *prototype* bucket; validated)
    #   "nomads_para" -> com/refs/para       + "ensprod"  (NOMADS pre-implementation parallel)
    #   "nomads_prod" -> com/refs/prod       + "ensprod"  (NOMADS production, live 2026-08-31 12Z)
    # CUTOVER: flip this to "nomads_prod" once the NOMADS REFS feed is confirmed reachable and its
    # ``ensprod`` layout validated from a network-unrestricted host. Default "aws" is the only feed
    # reachable for end-to-end validation pre-cutover.
    refs_source: Literal["aws", "nomads_para", "nomads_prod"] = "aws"
    # Raw overrides (take precedence over ``refs_source``); leave None to use the profile above.
    refs_base_url: str | None = None
    # Ensemble-product subdir override ("enspost" on AWS, "ensprod" on NOMADS).
    refs_subdir: str | None = None

    # The NWS API (api.weather.gov) requires a self-identifying User-Agent with a
    # contact (FR-5). Override via UPSTREAMWX_NWS_USER_AGENT to your own contact.
    nws_user_agent: str = "UpstreamWX/0.1 (+https://upstreamwx.com)"

    # Number of recent GEFS cycles to retain in the on-disk member cache before pruning.
    # 4 covers ~1 day of GEFS's four daily cycles (00/06/12/18Z).
    gefs_cache_keep_cycles: int = 4

    # Freshness bound (hours since run init) for serving a cached ensemble cycle as "current".
    # Both ensembles run 6-hourly with a ~4-8 h publication lag, so a healthy system serves runs
    # <= ~14 h old; 24 h tolerates a missed warm without letting a stalled scheduler or a
    # long-idle CLI data_dir quietly present days-old members as the live ensemble. Past the
    # bound GEFS falls through to a live NOMADS probe and REFS degrades to "unavailable" with
    # an explicit note (data quality first-class, NFR-6).
    ensemble_max_age_h: float = 24.0

    # Number of recent REFS *runs* (00/06/12/18Z) to retain in the on-disk grid cache. 3
    # guarantees the previous run is present to backfill the current run's spin-up hours
    # even on a missed scheduler tick or a late publish.
    refs_cache_keep_cycles: int = 3

    # GEFS forecast hours the scheduler (and deploy.sh) pre-warm each cycle. GEFS is per-member
    # (31×fields), so warming is heavy — but leaving it on-demand puts the full ~500-subset cold
    # ingest on the first briefing's critical path every cycle. Default to the f24-f120 / 6 h band
    # (the multi-day planning horizon GEFS owns beyond REFS's ~36 h window); warming is download-
    # only and fanned across a thread pool (:func:`upstreamwx.gefs.cache.warm_cycle`). Override
    # with UPSTREAMWX_GEFS_WARM_FHOURS=[...] (e.g. [] to revert to fully on-demand).
    gefs_warm_fhours: list[int] = Field(default_factory=lambda: list(range(24, 121, 6)))

    # Start the M0.3 API's background refresh scheduler on app startup (FR-12). Default
    # on for the always-on EC2 service; set UPSTREAMWX_API_ENABLE_SCHEDULER=0 to run the
    # API without the recurring loop (e.g. tests, or a worker-less deployment).
    api_enable_scheduler: bool = True

    # Start the watershed cache-warming pool on app startup so the mission planner can
    # pre-delineate the upstream basin the moment coordinates change, hiding the 3-15 s
    # cold trace behind the user's mission-entry time (FR-3). Default on for the always-on
    # service; set UPSTREAMWX_API_ENABLE_WARM=0 for tests or a worker-less deployment.
    api_enable_warm: bool = True

    # Decode GEFS GRIB2 in a persistent process pool (spawn) so the per-member decodes run truly
    # in parallel — eccodes is not thread-safe, so the in-process path serializes them on a global
    # lock. **Opt-in (default off):** each spawn worker is a fresh interpreter that re-imports the
    # whole scientific stack (xarray + cfgrib/eccodes + regionmask/rasterio, and the package chain
    # pulls in timezonefinder) — ~300–500 MB RSS *per worker* on top of the main process. On the
    # small (≤2 GB) production host that OOM-killed uvicorn (→ nginx 502). Only enable
    # (UPSTREAMWX_API_ENABLE_DECODE_POOL=1, and tune UPSTREAMWX_DECODE_POOL_WORKERS) on a host with
    # real RAM headroom; otherwise the single-interpreter in-process decode is the safe path and
    # GEFS *warming* (off the request path) is the latency win.
    api_enable_decode_pool: bool = False
    # Worker count for the decode pool when enabled. Keep small — see the RSS-per-worker note above.
    decode_pool_workers: int = Field(default_factory=lambda: min(4, (os.cpu_count() or 2)))
    # Resident byte budget for the in-process decoded-grid LRU (memory-aware eviction). GEFS decodes
    # are cropped at decode time (~KB each), so this mainly bounds the larger REFS native grids; a
    # conservative 128 MiB keeps the resident cache safe on a ≤2 GB host (raise where RAM allows).
    # Replaces the old flat 48-entry count cap that retained full grids.
    decode_cache_max_bytes: int = 128 * 1024 * 1024

    # Hard entry cap for the in-process briefing cache and the engine-result store (M0.3).
    # Both are keyed by mission and were previously unbounded dicts — on an always-on host
    # they grew with every distinct mission for the process lifetime (a slow leak). Bounding
    # them as LRUs caps memory; an evicted entry simply regenerates on the next request, and
    # an evicted engine result makes the streaming frame endpoint a graceful miss (NFR-6).
    # 512 distinct missions is generous for a single-host beta; raise where RAM allows.
    api_cache_max_entries: int = 512

    # Resident byte budget for the in-process briefing + result caches (SA-02). The entry-count
    # cap alone does not bound memory: one large mission (name/route_note) or a big rendered
    # briefing, multiplied across N entries, can retain gigabytes despite a modest count cap.
    # The caches now evict LRU until BOTH the entry cap and this byte budget hold. With mission
    # and inputs fields bounded at the request boundary, each entry is small; this guarantees the
    # ceiling even under a max-legal-size load test. 256 MiB is safe on a >=2 GiB host.
    api_cache_max_bytes: int = 256 * 1024 * 1024

    # Cap the active-mission refresh registry (H-8). refresh_active re-ingests every registered
    # mission each cycle, so scheduler cost scales linearly with the registry — which previously
    # grew without bound (entries only dropped when their window ended). At the cap the service
    # evicts the mission whose window ends soonest; an evicted mission still briefs on demand,
    # it just loses scheduled refresh (NFR-6). 256 in-range missions is generous for one host.
    # NOTE (SA-03): with the recently-viewed TTL and per-pass budget below, this is now a MEMORY
    # ceiling on the registry dict, not the refresh WORK bound — the work is bounded by those two
    # controls regardless of registry size. Lower it if a host wants a tighter registry.
    api_active_missions_max: int = 256

    # How long since it was last VIEWED a mission stays eligible for scheduled refresh (SA-03).
    # Every successful /v1/briefing (cache hit OR miss) bumps the mission's last-seen; a refresh
    # regeneration does NOT (else refresh would keep itself alive forever). So a fire-and-forget
    # request stops being refreshed after this window — one anonymous request becomes at most
    # ~2 cycles of recurring work, not the up-to-17-days a 10-day-out, 7-day window allowed. A
    # reopened (actively planned) mission stays warm. Default 12 h ≈ two 6-hourly cycles; a
    # pruned mission still briefs on demand (NFR-6). 0 disables the gate (refresh until window end).
    api_active_refresh_ttl_s: float = 12 * 3600.0

    # Hard per-pass caps for the scheduled refresh (SA-03) so one pass can never run unbounded
    # work or starve interactive briefings. The pass stops cleanly at this many regenerations
    # (0 = unlimited) or this many wall-clock seconds (0 = unlimited), whichever comes first;
    # missions not reached simply refresh next cycle or on demand (NFR-6). Defaults suit a small
    # host: 64 items well above a healthy recently-viewed registry, 240 s a generous ceiling
    # given warm GEFS/REFS + watershed caches make each regeneration a few seconds.
    api_refresh_pass_max_items: int = 64
    api_refresh_pass_max_seconds: float = 240.0

    # How long a refresh regeneration waits for a shared generation slot (briefing_max_concurrency)
    # before yielding the rest of the pass to interactive briefings (SA-03). Scheduled and request
    # generations share _gen_sem so they never jointly exceed the concurrency cap; a short wait
    # means refresh only uses spare capacity — if real users hold the slots, the pass defers rather
    # than competing. Deferred missions refresh next cycle (NFR-6).
    api_refresh_gen_wait_s: float = 0.5

    # Cap the watershed warm queue (H-8): each pending warm is a 3-15 s USGS delineation, and the
    # pending set previously grew (and queued executor work) without bound. At the cap the service
    # refuses new warms (endpoint -> 503 + Retry-After); the briefing then just pays the cold
    # trace itself (NFR-6). 32 pending points is far beyond any legitimate planner burst.
    api_warm_pending_max: int = 32

    # Per-IP token-bucket rate limiting on the expensive/billable endpoints (frame/pdf/warm),
    # H-8. In-process and dependency-free; nginx's edge limit_req still applies in front
    # (deploy/nginx/upstreamwx.conf) — this is the app's own defence when reached directly.
    # Set UPSTREAMWX_API_RATE_LIMITS_ENABLED=0 to disable (e.g. load tests).
    api_rate_limits_enabled: bool = True

    # Per-IP budget for cold /v1/briefing GENERATIONS (cache MISSES), SA-02. Cache hits are free
    # and uncounted; only work that spends live ingest is charged, so reopening the app or a
    # scheduled refresh never draws on this budget. Complements the nginx edge limit and the
    # briefing_max_concurrency cap with a per-principal COST budget (the edge/concurrency caps
    # bound rate and simultaneity, not how much cold work one client can force over time).
    # ~10/min is generous for a real planning session (each distinct mission is one miss) yet
    # caps abuse. 0 disables. Gated by api_rate_limits_enabled.
    api_briefing_miss_rate_per_min: float = 10.0

    # Cap concurrent live briefing GENERATIONS (the cold, memory/CPU-heavy ingest path) so a burst
    # of simultaneous distinct missions can't OOM/thrash a small host — excess requests wait briefly
    # for a slot, then return a fast 503 "busy, retry" (the PWA shows a retry banner) instead of all
    # spiking at once. Cache hits never count against this. ~2 suits a 2-vCPU/4 GiB box; raise where
    # there is headroom. Set 0 to disable the cap.
    briefing_max_concurrency: int = 2
    # Seconds a queued generation waits for a free slot before returning 503. A short wait lets a
    # quick collision resolve (and possibly hit the cache the in-flight request fills); past it the
    # client is told to retry rather than risk overrunning the gateway timeout (a 504).
    briefing_busy_timeout_s: float = 2.0

    # Accept the offline HazardInputs replay path on the public API (FR-25). Ordinary PWA users
    # never send `inputs`; it is a dev/corpus/CLI affordance that skips live ingest and creates
    # non-expiring static cache entries (SA-02). Default on for CLI/dev parity; set
    # UPSTREAMWX_API_ALLOW_INPUTS_REPLAY=0 on the public beta so an anonymous client cannot pin
    # cheap, durable cache entries (the durable half of the SA-02 memory-exhaustion vector).
    api_allow_inputs_replay: bool = True

    # TTL (seconds) for deterministic static (inputs-replay) cache entries (SA-02). They
    # previously never expired (cache.py STATIC_TOKEN), so a pinned replay entry could persist
    # for the whole process lifetime. Bounding their lifetime is belt-and-suspenders behind
    # api_allow_inputs_replay=0. Live (cycle-scoped) entries are unaffected — they already
    # expire on the next ensemble cycle (NFR-6).
    api_static_entry_ttl_s: float = 3600.0

    # App-level request-byte cap for the JSON mission endpoints (/v1/briefing, /frame, /warm),
    # SA-02. A real MissionSpec is a few KB even with a full inputs vector; anything larger is an
    # abuse payload. Enforced IN-APP (not only via nginx client_max_body_size) so the standalone
    # uvicorn entry point and a drifted edge config are both covered. The PDF endpoint keeps its
    # own larger 2 MiB cap.
    api_max_request_bytes: int = 64 * 1024

    # Dead-man's-switch monitoring (Healthchecks.io or any ping-on-success service). When
    # set, the GEFS/REFS + AFD refresh scheduler pings this URL each cycle (".../start" before the
    # pass, the base URL on success, ".../fail" on error). A stalled scheduler — stale
    # briefings with no error, the most dangerous failure mode on the always-on host — then
    # raises an alert instead of going unnoticed. Unset -> no pings. The ping URL is a
    # secret; keep it in the env file, never in git.
    healthcheck_url: str | None = None

    # Anthropic API key for the M0.2 SITREP Haiku framing layer (FR-21). Read from the
    # standard ANTHROPIC_API_KEY name; the validation_alias bypasses the env_prefix above.
    anthropic_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY")
    )

    # ── Public-release access gate: anonymous fair-use sessions (SA-01) ───────────────────
    # Master switch, default ON. But the gate is only ACTIVE when a signing secret is also set
    # (see session_secret) — this "secret-gated activation" is what lets on-by-default coexist
    # with the secretless contexts (dev, CLI, the hermetic test suite, the tailnet beta): no
    # secret → the gate stays inactive and /v1 runs open (with a startup WARNING), rather than
    # crashing. The public host just sets UPSTREAMWX_SESSION_SECRET and the gate turns itself on.
    # Set to 0 to force the gate off even where a secret exists (an operational kill-switch).
    # When active, every expensive /v1/* endpoint requires a valid session token and
    # per-principal + global cost budgets apply. The audit's point: IP-only throttling is "weak
    # identity"; this attaches budgets to an app-issued principal. See api/auth.py, api/budget.py.
    api_auth_enabled: bool = True

    # Fail-closed guard for production (default OFF). When set, a missing/blank session_secret
    # makes the app REFUSE to start instead of running open — so a public host that means to gate
    # can never silently ship unauthenticated because someone forgot the secret. Left off for
    # dev/CLI/tests/tailnet, which legitimately run open without a secret.
    api_auth_required: bool = False

    # HMAC signing secret for the stateless anonymous session tokens. Read as
    # UPSTREAMWX_SESSION_SECRET (32+ random bytes: `openssl rand -hex 32`); lives in the runtime
    # EnvironmentFile beside ANTHROPIC_API_KEY, never in git. Presence is what ACTIVATES the gate
    # (see api_auth_enabled): with it set the gate enforces, without it /v1 runs open (unless
    # api_auth_required forces a fail-closed startup). *_prev is verify-only, so the secret can be
    # rotated with zero session loss.
    session_secret: str | None = Field(
        default=None, validation_alias=AliasChoices("UPSTREAMWX_SESSION_SECRET")
    )
    session_secret_prev: str | None = Field(
        default=None, validation_alias=AliasChoices("UPSTREAMWX_SESSION_SECRET_PREV")
    )
    # Session lifetime (seconds). The PWA re-mints transparently on expiry, so a week keeps the
    # cookie stable across a multi-day expedition without a long-lived credential.
    session_ttl_s: int = 7 * 24 * 3600
    # Set the Secure attribute on the session cookie (default on — production is HTTPS-only, a
    # hard prerequisite, see SA-09). Set 0 only for local http/dev and the offline test client.
    session_cookie_secure: bool = True
    # Per-IP token-bucket budget for session MINTING (POST /v1/session). Minting is cheap for us
    # but freely mintable anonymous tokens are the weak point of the model, so cap it hard per IP.
    # Gated by api_rate_limits_enabled.
    session_mint_rate_per_min: float = 5.0

    # ── Per-principal fair-use budgets (only apply when api_auth_enabled) ─────────────────
    # Charged only on WORK, never on cache hits: reopening the app or re-requesting the same
    # mission is free. Per-IP token buckets (SA-02) remain the IP-aggregate layer beneath these,
    # so token rotation from one source is still bounded. 0 disables a given budget.
    budget_cold_per_principal_per_hour: int = 20  # cache-miss briefings (live ingest)
    budget_frame_per_principal_per_day: int = 30  # billable Anthropic framing calls
    budget_pdf_per_principal_per_hour: int = 20  # each launches headless Chromium
    budget_warm_per_principal_per_hour: int = 60  # each is a 3-15 s USGS delineation
    # Per-principal cap on scheduled-refresh registrations (SA-03): one client can no longer fill
    # the shared active-mission registry. Over quota still briefs on demand, it just gets no
    # recurring refresh (NFR-6). Well below api_active_missions_max (the global registry cap).
    budget_active_per_principal: int = 3

    # ── Global ceilings / circuit breakers (only apply when api_auth_enabled) ─────────────
    # Absolute host- and cost-protection independent of any single principal. The frame ceiling
    # is the model-spend cap: past it the API returns 503 + Retry-After and logs a WARNING (the
    # alerting hook). 0 disables a given ceiling.
    budget_global_cold_per_hour: int = 1200
    budget_global_frame_per_day: int = 2000

    # Expose FastAPI's interactive docs (/docs, /redoc, /openapi.json). Default OFF (SA-12):
    # production should not publish its full request surface. Dev/staging can set it to 1.
    docs_enabled: bool = False

    def ensure_data_dir(self) -> Path:
        """Create and return the data cache directory."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def get_settings() -> Settings:
    """Return a fresh :class:`Settings` instance (cheap; re-reads env)."""
    return Settings()
