"""Scheduled-refresh bounds and registry concurrency (SA-03).

The scheduler must never let one request become unbounded, multi-day, or interactive-
starving background work. These hermetic tests exercise the four controls added in SA-03:
the recently-viewed TTL, the per-pass item/wall-clock budget, sharing the generation
concurrency cap (yielding to interactive work), and the lock around the active registry.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from upstreamwx.api.cache import mission_cache_key
from upstreamwx.api.cycles import cycle_key
from upstreamwx.api.models import MissionSpec
from upstreamwx.api.service import BriefingService
from upstreamwx.engine.models import HazardInputs
from upstreamwx.sitrep import generate as generate_mod


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def _offline_service(monkeypatch) -> BriefingService:
    """A service whose generator never hits the network (forces empty inputs)."""
    real = generate_mod.generate_briefing

    def offline(mission, *, inputs=None, frame=None, generated_at=None, cycle=None, units="us"):
        return real(mission, inputs=inputs or HazardInputs(), frame=False,
                    generated_at=generated_at)

    monkeypatch.setattr("upstreamwx.api.service.generate_briefing", offline)
    service = BriefingService()
    monkeypatch.setattr(service, "_cycle_token", lambda now: cycle_key(now))
    return service


def _spec(**overrides) -> MissionSpec:
    # Window ~8 days out so it stays in range across the multi-hour refresh passes below
    # without tripping the 10-day start-lead cap in ensure_current.
    base = dict(
        lat=37.0, lon=-112.0, activity="canyon",
        start="2026-06-27T08:00", end="2026-06-27T18:00", frame=False,
    )
    base.update(overrides)
    return MissionSpec(**base)


# -- WS-3: recently-viewed TTL ---------------------------------------------------------------

def test_refresh_prunes_missions_not_recently_viewed(monkeypatch):
    """A fire-and-forget mission stops refreshing after api_active_refresh_ttl_s (not days)."""
    monkeypatch.setenv("UPSTREAMWX_API_ACTIVE_REFRESH_TTL_S", "43200")  # 12 h
    service = _offline_service(monkeypatch)
    t0 = _utc(2026, 6, 19, 12)

    service.get_briefing(_spec(name="abandoned"), now=t0)
    assert service.active_count == 1

    # A pass well within the TTL still refreshes it.
    assert service.refresh_active(now=t0 + timedelta(hours=6)) == 1
    assert service.active_count == 1

    # A pass past the TTL (no intervening view — a refresh is not a view) prunes it.
    assert service.refresh_active(now=t0 + timedelta(hours=13)) == 0
    assert service.active_count == 0
    assert service.last_refresh_stats.pruned_stale == 1


def test_view_bumps_last_seen_keeps_mission_warm(monkeypatch):
    """A view (_touch_active) resets the recently-viewed clock so the mission stays refreshed."""
    monkeypatch.setenv("UPSTREAMWX_API_ACTIVE_REFRESH_TTL_S", "43200")  # 12 h
    service = _offline_service(monkeypatch)
    t0 = _utc(2026, 6, 19, 12)
    service.get_briefing(_spec(name="active"), now=t0)
    key = mission_cache_key(_spec(name="active").to_mission())

    # Simulate the user reopening the app 12 h later (a cache hit bumps last_seen).
    service._touch_active(key, t0 + timedelta(hours=12))

    # A pass at t0+13h would prune from t0, but the view at t0+12h keeps it (delta 1 h < TTL).
    assert service.refresh_active(now=t0 + timedelta(hours=13)) == 1
    assert service.active_count == 1
    assert service.last_refresh_stats.pruned_stale == 0


def test_refresh_ttl_disabled_refreshes_until_window_end(monkeypatch):
    """TTL=0 disables the recently-viewed gate (refresh until the window ends)."""
    monkeypatch.setenv("UPSTREAMWX_API_ACTIVE_REFRESH_TTL_S", "0")
    service = _offline_service(monkeypatch)
    t0 = _utc(2026, 6, 19, 12)
    service.get_briefing(_spec(name="until-end"), now=t0)
    # Days later but still before the window: TTL off -> still refreshed.
    assert service.refresh_active(now=t0 + timedelta(days=5)) == 1


# -- WS-4: per-pass item / wall-clock budget -------------------------------------------------

def test_refresh_item_budget_caps_a_pass(monkeypatch):
    """A pass regenerates at most api_refresh_pass_max_items; the rest wait for the next pass."""
    monkeypatch.setenv("UPSTREAMWX_API_REFRESH_PASS_MAX_ITEMS", "2")
    service = _offline_service(monkeypatch)
    t0 = _utc(2026, 6, 19, 12)

    for i in range(5):
        service.get_briefing(_spec(name=f"m{i}", lat=37.0 + i * 0.1), now=t0)
    assert service.active_count == 5

    regenerated = service.refresh_active(now=t0 + timedelta(hours=1))
    assert regenerated == 2
    stats = service.last_refresh_stats
    assert stats.regenerated == 2
    assert stats.skipped_budget == 3
    assert service.active_count == 5  # budget-skipped missions are NOT pruned — they refresh later


# -- WS-5: share the generation concurrency cap (yield to interactive work) -------------------

def test_refresh_defers_when_generation_slots_are_busy(monkeypatch):
    """When interactive briefings hold the generation slots, the pass defers, not competes."""
    monkeypatch.setenv("UPSTREAMWX_BRIEFING_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("UPSTREAMWX_API_REFRESH_GEN_WAIT_S", "0.05")  # keep the test fast
    service = _offline_service(monkeypatch)
    t0 = _utc(2026, 6, 19, 12)
    service.get_briefing(_spec(name="busy"), now=t0)

    # Simulate interactive load holding every generation slot.
    assert service._gen_sem is not None
    service._gen_sem.acquire()
    service._gen_sem.acquire()
    try:
        regenerated = service.refresh_active(now=t0 + timedelta(hours=1))
    finally:
        service._gen_sem.release()
        service._gen_sem.release()

    assert regenerated == 0
    assert service.last_refresh_stats.deferred == 1
    assert service.active_count == 1  # deferred, not pruned — it refreshes next cycle


# -- Per-mission resilience: one bad mission must not sink the pass (rec 7 / NFR-6) ----------

def test_refresh_survives_one_failing_mission(monkeypatch):
    """A mission whose generation raises is counted as failed; the rest still refresh."""
    service = _offline_service(monkeypatch)
    t0 = _utc(2026, 6, 19, 12)
    for i in range(3):
        service.get_briefing(_spec(name=f"m{i}", lat=37.0 + i * 0.1), now=t0)
    assert service.active_count == 3

    # Make exactly the middle mission's generation blow up.
    bad_key = mission_cache_key(_spec(name="m1", lat=37.1).to_mission())
    real = generate_mod.generate_briefing

    def flaky(mission, *, inputs=None, frame=None, generated_at=None, cycle=None, units="us"):
        if mission_cache_key(mission) == bad_key:
            raise RuntimeError("boom")
        return real(mission, inputs=HazardInputs(), frame=False, generated_at=generated_at)

    monkeypatch.setattr("upstreamwx.api.service.generate_briefing", flaky)

    regenerated = service.refresh_active(now=t0 + timedelta(hours=1))
    assert regenerated == 2  # the two healthy missions still refreshed
    assert service.last_refresh_stats.failed == 1  # the bad one counted, not fatal
    assert service.active_count == 3  # a failed regen does not drop the registration


# -- WS-2: registry lock under concurrency ---------------------------------------------------

def test_registry_survives_concurrent_register_touch_refresh(monkeypatch):
    """Simultaneous registration, touch, eviction, and refresh never raise and stay consistent."""
    service = _offline_service(monkeypatch)
    # A trivial generate stub so refresh threads spin fast without heavy engine work.
    monkeypatch.setattr(
        "upstreamwx.api.service.generate_briefing",
        lambda mission, *, inputs=None, frame=None, generated_at=None, cycle=None, units="us": (
            generate_mod.generate_briefing(
                mission, inputs=HazardInputs(), frame=False, generated_at=generated_at
            )
        ),
    )
    t0 = _utc(2026, 6, 19, 12)
    missions = [_spec(name=f"c{i}", lat=37.0 + i * 0.01).to_mission() for i in range(20)]
    keys = [mission_cache_key(m) for m in missions]
    errors: list[BaseException] = []

    def register():
        try:
            for m, k in zip(missions, keys, strict=True):
                service._register_active(k, m, now=t0)
        except BaseException as exc:  # noqa: BLE001 — collect for the assertion
            errors.append(exc)

    def touch():
        try:
            for k in keys:
                service._touch_active(k, t0)
                _ = service.active_count
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def refresh():
        try:
            for _ in range(10):
                service.refresh_active(now=t0 + timedelta(hours=1))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=fn) for fn in (register, touch, refresh) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, errors
    # The registry is internally consistent: every retained entry is a valid registration.
    with service._active_lock:
        for k, reg in service._active.items():
            assert reg.key == k
    assert service.active_count <= service._active_max
