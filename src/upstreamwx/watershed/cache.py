"""On-disk cache for HUC resolution + upstream tracing (M0.1 promotion of Spike B).

The live USGS WBD trace is deterministic but slow (≈3 s for compact basins, up to
≈15 s for plains rivers needing region widening). M0.1 promotes it to a cached
module: a point's dissolved upstream domain is resolved once and reused. The cache
is a GeoJSON Feature per rounded lat/lon under ``Settings.data_dir/watershed``.

Note: this is a *process/disk* cache for on-demand use; the persistent
cross-restart cache the scheduler relies on is an M0.1.1 / EC2 concern.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from shapely.geometry import mapping, shape

from ..config import Settings, get_settings
from .huc import resolve_huc12
from .pourpoint import PourpointBasin, delineate
from .upstream import UpstreamTrace, trace_upstream

# Rounding precision for the cache key (~100 m at CONUS latitudes is plenty for a
# HUC-12 domain, which is far larger).
_KEY_PRECISION = 3


def _cache_dir(settings: Settings) -> Path:
    d = settings.data_dir / "watershed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(lat: float, lon: float) -> str:
    return f"{round(lat, _KEY_PRECISION)}_{round(lon, _KEY_PRECISION)}"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically so a concurrent reader never sees a
    half-written file (NFR-6). Watershed warming may delineate the same basin while a
    briefing reads/writes it; an os.replace of a temp file makes the swap indivisible.
    """
    # A unique temp name (not a fixed ".tmp" suffix) so two concurrent writers to the same
    # key — e.g. a refresh racing the single-flight owner — never clobber each other's temp.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _to_feature(trace: UpstreamTrace) -> dict:
    return {
        "type": "Feature",
        "geometry": mapping(trace.polygon),
        "properties": {
            "origin_huc12": trace.origin_huc12,
            "upstream_huc_ids": trace.upstream_huc_ids,
            "area_km2": trace.area_km2,
            "method": trace.method,
            "huc_level": trace.huc_level,
            "notes": trace.notes,
        },
    }


def _from_feature(feature: dict) -> UpstreamTrace:
    props = feature["properties"]
    return UpstreamTrace(
        origin_huc12=props["origin_huc12"],
        upstream_huc_ids=props["upstream_huc_ids"],
        polygon=shape(feature["geometry"]),
        area_km2=props["area_km2"],
        method=props["method"],
        huc_level=props.get("huc_level", 12),
        notes=props.get("notes", []),
    )


def _resolve_and_trace(lat: float, lon: float, *, retries: int = 3) -> UpstreamTrace:
    """Live resolve + trace with simple backoff on transient USGS failures."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return trace_upstream(resolve_huc12(lat, lon))
        except Exception as exc:  # transient WFS 403/502, timeouts (Spike B notes)
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"watershed trace failed for {lat},{lon}: {last_exc}") from last_exc


def resolve_and_trace_cached(
    lat: float,
    lon: float,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> UpstreamTrace:
    """Return the upstream trace for a point, using the on-disk cache when present."""
    settings = settings or get_settings()
    path = _cache_dir(settings) / f"{_key(lat, lon)}.geojson"

    if path.is_file() and not refresh:
        return _from_feature(json.loads(path.read_text()))

    trace = _resolve_and_trace(lat, lon)
    _atomic_write(path, json.dumps(_to_feature(trace)))
    return trace


# --------------------------------------------------------------------------- #
# Pour-point basin cache (NLDI raindrop two-step, with WBD fallback).
# --------------------------------------------------------------------------- #
# Single-flight registry: at most one live delineation per cache key. A briefing that
# arrives while a planner-triggered warm is still tracing the same point joins the
# in-flight Future instead of starting a duplicate 3-15 s NLDI/USGS trace. This is what
# makes "warm the cache the moment coordinates are entered" safe for the quick user who
# generates before the warm finishes.
_inflight_lock = threading.Lock()
_inflight: dict[str, Future[PourpointBasin]] = {}


def _pourpoint_dir(settings: Settings) -> Path:
    d = settings.data_dir / "watershed" / "pourpoint"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _basin_to_feature(basin: PourpointBasin) -> dict:
    return {
        "type": "Feature",
        "geometry": mapping(basin.polygon),
        "properties": {
            "lat": basin.lat,
            "lon": basin.lon,
            "snapped_lat": basin.snapped_lat,
            "snapped_lon": basin.snapped_lon,
            "area_km2": basin.area_km2,
            "method": basin.method,
            "comid": basin.comid,
            "flowline_name": basin.flowline_name,
            "notes": basin.notes,
        },
    }


def _basin_from_feature(feature: dict) -> PourpointBasin:
    props = feature["properties"]
    return PourpointBasin(
        lat=props["lat"],
        lon=props["lon"],
        snapped_lat=props["snapped_lat"],
        snapped_lon=props["snapped_lon"],
        polygon=shape(feature["geometry"]),
        area_km2=props["area_km2"],
        method=props["method"],
        comid=props.get("comid"),
        flowline_name=props.get("flowline_name"),
        notes=props.get("notes", []),
    )


def delineate_cached(
    lat: float,
    lon: float,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> PourpointBasin:
    """Return the pour-point basin for a point, using the on-disk cache when present.

    Concurrent callers for the same point (e.g. a background warm and the briefing that
    needs it) are coalesced: the first becomes the owner and runs the live delineation;
    the rest wait on its result. ``refresh=True`` always starts a fresh trace rather than
    joining an in-flight one.
    """
    settings = settings or get_settings()
    key = _key(lat, lon)
    path = _pourpoint_dir(settings) / f"{key}.geojson"

    # Fast path: a warm disk file. Lock-free — read-only.
    if path.is_file() and not refresh:
        return _basin_from_feature(json.loads(path.read_text()))

    # Decide owner vs waiter under the registry lock.
    with _inflight_lock:
        # Re-check the disk inside the lock: the owner may have finished and written the
        # file between our fast-path check above and acquiring the lock.
        if path.is_file() and not refresh:
            return _basin_from_feature(json.loads(path.read_text()))
        existing = _inflight.get(key)
        if existing is not None and not refresh:
            fut, owner = existing, False
        else:
            fut, owner = Future(), True
            _inflight[key] = fut

    if not owner:
        return fut.result()  # blocks; re-raises the owner's exception if it failed

    # The slow trace runs outside the lock so waiters never block the registry.
    try:
        basin = delineate(lat, lon)
        _atomic_write(path, json.dumps(_basin_to_feature(basin)))
        fut.set_result(basin)
        return basin
    except BaseException as exc:
        fut.set_exception(exc)
        raise
    finally:
        with _inflight_lock:
            # Identity check: a concurrent refresh may have replaced our entry.
            if _inflight.get(key) is fut:
                del _inflight[key]
