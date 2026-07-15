"""Briefing service: cache-aware generation + the active-mission registry (M0.3).

This is the seam between the HTTP layer and the M0.2 generation core. It:

- serves a cached briefing when one is valid for the current cycle, else generates and
  caches (PRD §7, §11) — so reopening the app is free;
- registers in-range live missions so the scheduler can regenerate them on the SREF/AFD
  cycle (FR-12, :mod:`upstreamwx.api.scheduler`);
- always returns a briefing, marking unavailable sources rather than failing (NFR-6).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .. import gefs, refs
from ..config import get_settings
from ..engine.models import BriefingResult, Mission
from ..sitrep.generate import GeneratedBriefing, generate_briefing
from ..sitrep.structured import to_structured
from ..watershed.cache import _key as watershed_key
from ..watershed.cache import delineate_cached
from .auth import auth_active
from .cache import STATIC_TOKEN, BoundedLRU, BriefingCache, _estimate_bytes, mission_cache_key
from .cycles import cycle_key
from .models import BriefingResponse, MissionSpec

logger = logging.getLogger("upstreamwx.api.service")

# How long a live-probe cycle token is reused before re-probing NOMADS (seconds). Keeps the
# availability check off the per-request path while still noticing a newly published run
# within minutes of it appearing.
_TOKEN_TTL_S = 300.0


class BriefingBusy(Exception):
    """Raised when the concurrent-generation cap is saturated (maps to HTTP 503).

    Signals the request path that no generation slot freed within the busy timeout, so the API
    should tell the client to retry shortly rather than pile another cold ingest onto a host that
    is already at capacity (the PWA surfaces a retry banner).
    """


class WarmQueueFull(Exception):
    """Raised when the watershed warm queue is saturated (maps to HTTP 503 + Retry-After, H-8).

    Each pending warm is a 3-15 s USGS delineation; refusing past the cap is backpressure the
    fire-and-forget endpoint previously lacked. A refused warm costs nothing but the latency
    win — the next briefing simply pays the cold trace itself (NFR-6).
    """


class InputsReplayDisabled(Exception):
    """Raised when an ``inputs`` replay is requested but disabled on this server (SA-02 → 403).

    The offline HazardInputs replay path (FR-25) skips live ingest and creates non-expiring
    static cache entries — the durable half of the SA-02 memory-exhaustion vector. Ordinary PWA
    users never send ``inputs``, so the public beta sets ``api_allow_inputs_replay=0`` and the
    service refuses such requests before any work; CLI/dev keep it on for reproducible replays.
    """


@dataclass
class _Registered:
    """A live mission tracked for scheduled refresh while its window is in range."""

    mission: Mission
    frame: bool | None
    key: str
    pid: str | None = None  # owning principal (SA-01/SA-03), None when the gate is off
    last_seen: datetime | None = None  # last time it was VIEWED (SA-03 recently-viewed gate)


@dataclass(frozen=True)
class RefreshStats:
    """Outcome of one scheduled refresh pass, for logging and /v1/health (SA-03 rec 7).

    Counts only (no mission content), so it is safe to surface on the unauthenticated health
    probe (SA-12). ``deferred`` is work yielded to interactive briefings on generation-slot
    contention; ``skipped_budget`` is work left for the next pass by the item/wall-clock cap —
    both are refreshed next cycle or on demand (NFR-6), never dropped.
    """

    registry_size: int = 0
    regenerated: int = 0
    pruned_ended: int = 0
    pruned_stale: int = 0
    deferred: int = 0
    skipped_budget: int = 0
    duration_s: float = 0.0


class BriefingService:
    """Orchestrates caching, generation, and refresh registration for briefings."""

    def __init__(self, cache: BriefingCache | None = None) -> None:
        settings = get_settings()
        maxsize = settings.api_cache_max_entries
        # Bound the briefing cache on BOTH axes (SA-02): entry count and estimated retained
        # bytes, with a TTL on deterministic static (inputs-replay) entries so a pinned replay
        # cannot persist for the process lifetime. Count caps alone don't bound memory.
        self.cache = cache or BriefingCache(
            maxsize=maxsize,
            maxbytes=settings.api_cache_max_bytes,
            static_ttl_s=settings.api_static_entry_ttl_s,
        )
        # Bound concurrent cold generations so a burst can't OOM/thrash a small host. A value <= 0
        # disables the cap (an effectively unbounded semaphore). Cache hits never acquire it.
        self._gen_busy_timeout = settings.briefing_busy_timeout_s
        n = settings.briefing_max_concurrency
        self._gen_sem = threading.BoundedSemaphore(n) if n and n > 0 else None
        # Refresh registry, bounded (H-8): refresh_active re-ingests every entry each cycle,
        # so its cost scales linearly with this dict — which previously grew without bound.
        # Mutated from BOTH request worker threads (_register_active / _touch_active) and the
        # scheduler thread (refresh_active), so every access is guarded by _active_lock (SA-03):
        # the previous lock-free access could raise or refresh inconsistently under concurrency.
        self._active: dict[str, _Registered] = {}
        self._active_lock = threading.Lock()
        self._active_max = max(1, settings.api_active_missions_max)
        # Last scheduled-refresh pass outcome, for logging + /v1/health (SA-03 rec 7).
        self._last_refresh_stats = RefreshStats()
        self._warm_pending_max = max(1, settings.api_warm_pending_max)
        # Stores the engine BriefingResult alongside its cache key so the streaming
        # framing endpoint can call Haiku without re-running ingest. LRU-bounded like the
        # briefing cache so it cannot grow unbounded on an always-on host; an evicted result
        # makes the frame endpoint a graceful miss (it re-generates on the next briefing).
        self._result_store: BoundedLRU[BriefingResult] = BoundedLRU(
            maxsize, maxbytes=settings.api_cache_max_bytes
        )
        # Watershed warming: a bounded pool fills the pour-point cache in the background
        # the moment the planner reports a new point, de-duped by cache key so a dragged
        # marker can't flood it. Created in start_warming() (app lifespan), not at import.
        self._warm_pool: ThreadPoolExecutor | None = None
        self._warm_pending: set[str] = set()
        self._warm_lock = threading.Lock()
        # TTL-cached availability token (see _cycle_token). (monotonic stamp, token).
        self._token_lock = threading.Lock()
        self._token_cached: tuple[float, str] | None = None

    # -- cycle-validity token ---------------------------------------------------------
    def _cycle_token(self, now: datetime) -> str:
        """Cache-validity token from the newest ensemble cycle actually *available*.

        The wall-clock boundary (``cycle_key``) ignored publication lag: at 12:10Z it
        declared the token ``T12Z`` while the freshest data on NOMADS (and in the local
        cache) was still the 06Z run — so a briefing built from 06Z data was keyed, and
        advertised, as 12Z-fresh (data quality first-class: the token must track the data,
        not the clock). Resolution order: a fresh-enough warmed cycle on disk (cheap,
        hermetic — and exactly what the GEFS provider itself will read), else a live
        NOMADS probe memoised for ``_TOKEN_TTL_S``, else the wall-clock boundary as the
        last-resort fallback so caching still functions with the feed dark (NFR-6).
        """
        settings = get_settings()
        max_age = timedelta(hours=settings.ensemble_max_age_h)
        cached = gefs.cached_cycles(now=now, settings=settings)
        if cached and now - cached[0].init_time <= max_age:
            return cached[0].init_time.strftime("%Y-%m-%dT%HZ")
        with self._token_lock:
            if (
                self._token_cached is not None
                and time.monotonic() - self._token_cached[0] < _TOKEN_TTL_S
            ):
                return self._token_cached[1]
        live = gefs.latest_available_cycle(now=now)
        token = live.init_time.strftime("%Y-%m-%dT%HZ") if live is not None else cycle_key(now)
        with self._token_lock:
            self._token_cached = (time.monotonic(), token)
        return token

    # -- request path ---------------------------------------------------------------
    def get_briefing(
        self,
        spec: MissionSpec,
        *,
        now: datetime | None = None,
        on_miss: Callable[[], None] | None = None,
        principal_pid: str | None = None,
    ) -> BriefingResponse:
        """Return a briefing for ``spec``, from cache when valid or freshly generated.

        Raises :class:`~upstreamwx.api.models.MissionWindowError` (→ 422) for a live window
        outside the serviceable horizon, before any ingest cost is spent (H-8); offline
        ``inputs`` replays are exempt (FR-25). Raises :class:`InputsReplayDisabled` (→ 403) when
        ``inputs`` is supplied but the replay path is disabled on this server (SA-02).

        ``on_miss`` (SA-02) is invoked once a cache miss is confirmed and *before* any cold
        generation is spent, so the caller can charge a per-principal cost budget (the miss rate
        limit) that cache hits never touch. It may raise to abort the cold path.

        ``principal_pid`` (SA-01/SA-03) is the owning client's principal id. When the access gate
        is on it caps scheduled-refresh registrations per principal, so one client cannot fill the
        shared active-mission registry; the mission still briefs, it just gets no recurring refresh.
        """
        now = now or datetime.now(UTC)
        # Refuse the offline replay path when disabled (SA-02) — before any work, including the
        # wall-clock exemption ensure_current() would grant an inputs spec.
        if spec.inputs is not None and not get_settings().api_allow_inputs_replay:
            raise InputsReplayDisabled()
        spec.ensure_current(now)
        mission = spec.to_mission()
        inputs = spec.to_inputs()
        key = mission_cache_key(mission, inputs)
        # Explicit inputs are deterministic -> never expire; live briefings are valid
        # for the newest *available* GEFS/REFS cycle (FR-12) — see _cycle_token.
        token = STATIC_TOKEN if inputs is not None else self._cycle_token(now)

        cached = self.cache.get(key, token)
        if cached is not None:
            # A hit means the user reopened this mission — keep it eligible for scheduled
            # refresh (SA-03 recently-viewed gate) without charging any cost.
            self._touch_active(key, now)
            return self._response(cached, token, cached=True)

        # Confirmed cache miss -> cold generation. Charge the caller's cost budget first (SA-02):
        # the hits above are free, so only work that spends live ingest is rate-limited. Raising
        # here (e.g. 429) aborts before the generation semaphore is even acquired.
        if on_miss is not None:
            on_miss()

        # Cap concurrent cold generations: a burst of distinct missions would otherwise each spin up
        # the full ingest (watershed delineation + GEFS/REFS), spiking memory/CPU enough to OOM a
        # small host. Wait briefly for a slot; if none frees, raise BriefingBusy (-> 503) so the
        # client retries rather than piling on. Cache hits above never reach here.
        if self._gen_sem is not None and not self._gen_sem.acquire(
            timeout=self._gen_busy_timeout
        ):
            raise BriefingBusy()
        try:
            # A request for the same mission may have generated it while we waited for a slot.
            cached = self.cache.get(key, token)
            if cached is not None:
                self._touch_active(key, now)
                return self._response(cached, token, cached=True)

            # Haiku framing is always deferred to the streaming /v1/briefing/frame endpoint
            # so the structured posture data is returned immediately without waiting for the
            # LLM call.  The engine result is stored in _result_store for the frame endpoint.
            briefing = generate_briefing(mission, inputs=inputs, frame=False, generated_at=now)
            self._result_store.put(key, briefing.result, size=_estimate_bytes(briefing))
            self.cache.put(key, briefing, token)
            # Track live, still-in-range missions for the scheduler (FR-12). Deterministic
            # offline briefings need no refresh, so they are not registered.
            if inputs is None and now < _as_utc(mission.window_end):
                self._register_active(key, mission, now=now, principal_pid=principal_pid)
            return self._response(briefing, token, cached=False)
        finally:
            if self._gen_sem is not None:
                self._gen_sem.release()

    def _register_active(
        self,
        key: str,
        mission: Mission,
        *,
        now: datetime,
        principal_pid: str | None = None,
    ) -> None:
        """Register a live mission for scheduled refresh, capped (FR-12, H-8, SA-01/SA-03).

        Refresh cost scales linearly with ``_active`` and it previously grew unboundedly
        (entries only dropped once their window ended). At the cap, evict the entry whose
        window ends soonest: it expires — and would be pruned by ``refresh_active`` —
        first, so it loses the least scheduled-refresh coverage; an evicted mission still
        briefs normally on demand (NFR-6).

        SA-03: when the access gate is on, a principal that already owns
        ``budget_active_per_principal`` in-range registrations gets no new one — one client can
        no longer convert single requests into unbounded recurring background work. The briefing
        itself is unaffected (it was already generated); it just isn't scheduled for refresh.
        ``last_seen`` is stamped with ``now`` so the recently-viewed gate (:meth:`refresh_active`)
        starts its clock at first request. Runs under ``_active_lock`` (SA-03) — the registry is
        also mutated from the scheduler thread.
        """
        settings = get_settings()
        with self._active_lock:
            if (
                auth_active(settings)
                and principal_pid is not None
                and key not in self._active
            ):
                owned = sum(1 for reg in self._active.values() if reg.pid == principal_pid)
                if owned >= settings.budget_active_per_principal:
                    return  # over per-principal quota — do not create recurring refresh work
            if key not in self._active and len(self._active) >= self._active_max:
                soonest = min(
                    self._active, key=lambda k: _as_utc(self._active[k].mission.window_end)
                )
                del self._active[soonest]
            self._active[key] = _Registered(
                mission=mission, frame=False, key=key, pid=principal_pid, last_seen=now
            )

    def _touch_active(self, key: str, now: datetime) -> None:
        """Bump a registered mission's ``last_seen`` because it was just viewed (SA-03).

        A cache hit means the user reopened the app for this mission, so it stays eligible for
        scheduled refresh; a mission never re-viewed goes stale and is pruned by
        :meth:`refresh_active`. No-op if the key is not registered (e.g. an ``inputs`` replay,
        or a registration skipped by the per-principal quota). O(1) under ``_active_lock``.
        """
        with self._active_lock:
            reg = self._active.get(key)
            if reg is not None:
                reg.last_seen = now

    def get_result(self, key: str) -> BriefingResult | None:
        """Return the cached engine result for ``key``, or None on miss.

        Used by the streaming frame endpoint: the ingest pipeline has already run
        for the briefing, so only the Haiku call remains.
        """
        return self._result_store.get(key)

    # -- scheduled refresh ----------------------------------------------------------
    def warm_and_prune(self, *, now: datetime | None = None) -> int:
        """Pre-pull the live GEFS + REFS cycles into the persistent cache and prune old ones.

        Run by the scheduler each cycle boundary before :meth:`refresh_active`. REFS's published
        forecast hours are warmed so a mission's spin-up hours are served from a prior run's
        mature forecast. GEFS is per-member and heavy, so it is warmed only for the bounded
        ``gefs_warm_fhours`` lead band (empty by default → GEFS serves on demand via its parallel
        cache-through fetch). Each ensemble is warmed independently — a missing/unpublished cycle
        for one does not block the other. Returns the total fields warmed; 0 when neither is live
        yet (production lag), which is non-fatal — refresh still runs from cache (NFR-6).
        """
        settings = get_settings()
        warmed = 0

        gcycle = gefs.latest_available_cycle(now=now)
        if gcycle is not None and settings.gefs_warm_fhours:
            warmed += len(
                gefs.warm_cycle(gcycle, tuple(settings.gefs_warm_fhours), settings=settings)
            )
            gefs.prune_old_cycles(settings=settings, keep=settings.gefs_cache_keep_cycles)

        rcycle = refs.latest_available_cycle(now=now)
        if rcycle is not None:
            warmed += len(refs.warm_cycle(rcycle, settings=settings))
            refs.prune_old_cycles(settings=settings, keep=settings.refs_cache_keep_cycles)

        return warmed

    def refresh_active(self, *, now: datetime | None = None) -> int:
        """Regenerate in-range, recently-viewed active missions into the current cycle (FR-12).

        Hardened per SA-03 so one pass can never run unbounded, multi-day, or interactive-
        starving work:

        - **Prune** (under ``_active_lock``): drop missions whose window has ended, and missions
          not *viewed* within ``api_active_refresh_ttl_s`` — a fire-and-forget request stops
          refreshing after the TTL (a refresh regeneration is not a view), so it becomes at most
          ~2 cycles of work, not days. A reopened (actively planned) mission stays warm.
        - **Budget**: stop cleanly at ``api_refresh_pass_max_items`` regenerations or
          ``api_refresh_pass_max_seconds`` wall-clock; the remainder refreshes next cycle or on
          demand (NFR-6).
        - **Yield**: each regeneration shares the request path's ``_gen_sem`` (so scheduled +
          interactive gens never exceed the concurrency cap) but waits only
          ``api_refresh_gen_wait_s`` for a slot; if the host is busy serving real users the pass
          defers the rest — refresh uses spare capacity only, never starving an interactive
          briefing.

        Returns the number regenerated (kept an ``int`` for callers); the full per-pass counts are
        on :attr:`last_refresh_stats`. Runs on the scheduler thread via ``asyncio.to_thread``.
        """
        now = now or datetime.now(UTC)
        settings = get_settings()
        token = self._cycle_token(now)
        ttl_s = settings.api_active_refresh_ttl_s  # <= 0 disables the recently-viewed gate
        stale_before = now - timedelta(seconds=ttl_s) if ttl_s and ttl_s > 0 else None
        max_items = settings.api_refresh_pass_max_items
        max_seconds = settings.api_refresh_pass_max_seconds
        gen_wait = settings.api_refresh_gen_wait_s
        start = time.monotonic()
        deadline = start + max_seconds if max_seconds and max_seconds > 0 else None

        # Prune ended/stale and snapshot the survivors under the lock; do the slow generation
        # OUTSIDE it so a long pass never blocks request-thread registration (SA-03).
        pruned_ended = pruned_stale = 0
        with self._active_lock:
            for key in list(self._active):
                reg = self._active[key]
                if now >= _as_utc(reg.mission.window_end):
                    del self._active[key]  # mission is over; stop refreshing it
                    pruned_ended += 1
                elif (
                    stale_before is not None
                    and reg.last_seen is not None
                    and reg.last_seen < stale_before
                ):
                    del self._active[key]  # not viewed within the TTL; stop refreshing it
                    pruned_stale += 1
            registry_size = len(self._active)
            # Soonest-ending first: imminent trips are the most likely to be acted on, so they
            # win the budget if the pass can't reach everyone.
            snapshot = sorted(
                ((k, reg.mission) for k, reg in self._active.items()),
                key=lambda km: _as_utc(km[1].window_end),
            )

        regenerated = deferred = skipped_budget = 0
        for i, (key, mission) in enumerate(snapshot):
            remaining = len(snapshot) - i
            if (max_items and regenerated >= max_items) or (
                deadline is not None and time.monotonic() >= deadline
            ):
                skipped_budget = remaining  # left for the next pass / on demand (NFR-6)
                break
            # Share the request concurrency cap and yield to interactive work: if no slot frees
            # promptly the host is busy with real briefings, so defer the rest of the pass.
            if self._gen_sem is not None and not self._gen_sem.acquire(timeout=gen_wait):
                deferred = remaining
                break
            try:
                briefing = generate_briefing(mission, frame=False, generated_at=now)
            finally:
                if self._gen_sem is not None:
                    self._gen_sem.release()
            self._result_store.put(key, briefing.result, size=_estimate_bytes(briefing))
            self.cache.put(key, briefing, token)
            regenerated += 1

        self._last_refresh_stats = RefreshStats(
            registry_size=registry_size,
            regenerated=regenerated,
            pruned_ended=pruned_ended,
            pruned_stale=pruned_stale,
            deferred=deferred,
            skipped_budget=skipped_budget,
            duration_s=round(time.monotonic() - start, 3),
        )
        return regenerated

    @property
    def active_count(self) -> int:
        with self._active_lock:
            return len(self._active)

    @property
    def last_refresh_stats(self) -> RefreshStats:
        """Counts from the most recent :meth:`refresh_active` pass (SA-03 rec 7)."""
        return self._last_refresh_stats

    # -- watershed warming ----------------------------------------------------------
    def start_warming(self, *, max_workers: int = 2) -> None:
        """Start the background watershed-warming pool (called from the app lifespan)."""
        if self._warm_pool is None:
            self._warm_pool = ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="ws-warm"
            )

    def stop_warming(self) -> None:
        """Tear the pool down without blocking shutdown on an in-flight trace."""
        pool, self._warm_pool = self._warm_pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def warm_watershed(self, lat: float, lon: float) -> bool:
        """Fire a background pour-point delineation for ``(lat, lon)`` (FR-3).

        Returns True if a warm was submitted, False if warming is disabled or an
        identical point is already in flight; raises :class:`WarmQueueFull` when the
        pending set is at ``api_warm_pending_max`` (H-8 backpressure — the endpoint maps
        it to 503 + Retry-After). ``radius_km`` is intentionally not a parameter:
        delineation depends only on the pour point, and the Radius-of-Concern clip is
        cheap post-processing the briefing applies later.
        """
        if self._warm_pool is None:
            return False
        key = watershed_key(lat, lon)
        with self._warm_lock:
            if key in self._warm_pending:
                return False
            # _warm_pending doubles as the executor's queue depth: past the cap, refuse
            # rather than queue unboundedly (each entry is a 3-15 s USGS delineation).
            if len(self._warm_pending) >= self._warm_pending_max:
                raise WarmQueueFull()
            self._warm_pending.add(key)
        self._warm_pool.submit(self._warm_job, lat, lon, key)
        return True

    def _warm_job(self, lat: float, lon: float, key: str) -> None:
        try:
            delineate_cached(lat, lon)  # populates the disk cache; joins single-flight
        except Exception:
            # Best-effort: a failed warm just means the briefing pays the cold cost (NFR-6).
            logger.exception("watershed warm failed for %s", key)
        finally:
            with self._warm_lock:
                self._warm_pending.discard(key)

    # -- helpers --------------------------------------------------------------------
    def _response(
        self, briefing: GeneratedBriefing, token: str, *, cached: bool
    ) -> BriefingResponse:
        # One serializer builds the whole structured contract (M0.4), including markdown.
        return BriefingResponse(**to_structured(briefing, cached=cached, cache_cycle=token))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
