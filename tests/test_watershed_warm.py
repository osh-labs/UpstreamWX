"""Watershed cache-warming tests (FR-3, latency follow-on to M0.1/M0.3).

Cold pour-point delineation (~3-15 s) is the dominant remaining briefing latency. The
mission planner warms it the moment coordinates change so the basin is ready by the time
the user generates the briefing. These tests cover the two correctness-critical pieces,
fully offline by monkeypatching the live :func:`delineate` seam:

- the single-flight registry in :func:`delineate_cached` coalesces a warm and the briefing
  that needs the same point into one trace (the "quick user" race);
- :class:`BriefingService.warm_watershed` fires that delineation in the background, de-duped
  by cache key, and a later briefing reads the warmed disk cache instead of re-tracing.
"""

from __future__ import annotations

import threading
import time

import pytest
from shapely.geometry import Polygon

from upstreamwx.api.service import BriefingService
from upstreamwx.config import Settings
from upstreamwx.watershed import cache as wscache
from upstreamwx.watershed.cache import _key, delineate_cached
from upstreamwx.watershed.pourpoint import PourpointBasin

# A point well away from the latency-test set so nothing collides.
LAT, LON = 38.5000, -109.5000


def _fake_basin(lat: float, lon: float) -> PourpointBasin:
    """A minimal valid basin around the query point (no network)."""
    poly = Polygon(
        [(lon - 0.01, lat - 0.01), (lon + 0.01, lat - 0.01),
         (lon + 0.01, lat + 0.01), (lon - 0.01, lat + 0.01)]
    )
    return PourpointBasin(
        lat=lat, lon=lon, snapped_lat=lat, snapped_lon=lon,
        polygon=poly, area_km2=poly.area, method="test-fake",
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    """The single-flight registry is process-wide; isolate each test."""
    with wscache._inflight_lock:
        wscache._inflight.clear()
    yield
    with wscache._inflight_lock:
        wscache._inflight.clear()


# --------------------------------------------------------------------------- #
# Single-flight coalescing in delineate_cached
# --------------------------------------------------------------------------- #
def test_single_flight_coalesces_concurrent_callers(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    calls = 0
    lock = threading.Lock()

    def slow_delineate(lat, lon):
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.3)  # hold the trace so waiters arrive mid-flight
        return _fake_basin(lat, lon)

    monkeypatch.setattr(wscache, "delineate", slow_delineate)

    n = 8
    barrier = threading.Barrier(n)
    results: list[PourpointBasin] = []
    rlock = threading.Lock()

    def worker():
        barrier.wait()
        basin = delineate_cached(LAT, LON, settings=settings)
        with rlock:
            results.append(basin)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == 1  # exactly one live trace despite n concurrent callers
    assert len(results) == n
    assert all(r.area_km2 == results[0].area_km2 for r in results)
    assert not wscache._inflight  # registry cleaned up


def test_exception_propagates_to_all_waiters(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)

    def failing_delineate(lat, lon):
        time.sleep(0.2)
        raise RuntimeError("nldi down")

    monkeypatch.setattr(wscache, "delineate", failing_delineate)

    n = 5
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    elock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            delineate_cached(LAT, LON, settings=settings)
        except BaseException as exc:  # noqa: BLE001 — we assert on it
            with elock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == n
    assert all(isinstance(e, RuntimeError) for e in errors)
    assert not wscache._inflight  # cleaned up even on failure


def test_refresh_does_not_join_inflight(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    calls = 0
    lock = threading.Lock()
    started = threading.Event()

    def slow_delineate(lat, lon):
        nonlocal calls
        with lock:
            calls += 1
        started.set()
        time.sleep(0.3)
        return _fake_basin(lat, lon)

    monkeypatch.setattr(wscache, "delineate", slow_delineate)

    t = threading.Thread(target=lambda: delineate_cached(LAT, LON, settings=settings))
    t.start()
    started.wait(timeout=2)  # the non-refresh owner is now mid-trace
    delineate_cached(LAT, LON, settings=settings, refresh=True)  # must not join it
    t.join()

    assert calls == 2  # refresh forced its own fresh trace


def test_disk_hit_skips_delineate(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    calls = 0

    def counting_delineate(lat, lon):
        nonlocal calls
        calls += 1
        return _fake_basin(lat, lon)

    monkeypatch.setattr(wscache, "delineate", counting_delineate)

    delineate_cached(LAT, LON, settings=settings)
    delineate_cached(LAT, LON, settings=settings)  # second call serves from disk

    assert calls == 1


# --------------------------------------------------------------------------- #
# BriefingService.warm_watershed
# --------------------------------------------------------------------------- #
def _wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_warm_returns_false_when_disabled():
    svc = BriefingService()
    assert svc.warm_watershed(LAT, LON) is False  # pool not started


def test_warm_dedups_pending_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    calls = 0
    lock = threading.Lock()
    release = threading.Event()

    def blocking_delineate(lat, lon):
        nonlocal calls
        with lock:
            calls += 1
        release.wait(timeout=3)
        return _fake_basin(lat, lon)

    monkeypatch.setattr(wscache, "delineate", blocking_delineate)

    svc = BriefingService()
    svc.start_warming()
    try:
        assert svc.warm_watershed(LAT, LON) is True
        assert _wait_until(lambda: calls == 1)  # first job is running
        # Same point, still pending -> not resubmitted.
        assert svc.warm_watershed(LAT, LON) is False
        release.set()
        assert _wait_until(lambda: _key(LAT, LON) not in svc._warm_pending)
        assert calls == 1
    finally:
        release.set()
        svc.stop_warming()


def test_warm_then_delineate_hits_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSTREAMWX_DATA_DIR", str(tmp_path))
    calls = 0

    def counting_delineate(lat, lon):
        nonlocal calls
        calls += 1
        return _fake_basin(lat, lon)

    monkeypatch.setattr(wscache, "delineate", counting_delineate)

    svc = BriefingService()
    svc.start_warming()
    try:
        assert svc.warm_watershed(LAT, LON) is True
        assert _wait_until(lambda: _key(LAT, LON) not in svc._warm_pending)
    finally:
        svc.stop_warming()

    assert calls == 1
    # The briefing path now reads the warmed disk cache — no second trace.
    delineate_cached(LAT, LON, settings=Settings(data_dir=tmp_path))
    assert calls == 1
