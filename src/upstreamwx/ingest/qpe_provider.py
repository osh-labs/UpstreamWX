"""MRMS observed-QPE antecedent-wetness provider (PRD §16.1 modifier, FR-3).

Antecedent wetness is a proxy for saturated soils / elevated baseflow across the upstream
contributing watershed — a *basin-integrated* quantity, not a point one. The original proxy
read Open-Meteo's model QPF at the single mission point, which under-catches localized
convection: a real multi-inch event over the basin can read as dry at one grid cell of one
model (observed at 34.63,-85.46 on 2026-07-04 — GFS-family QPF saw 0.02" where radar+gauge saw
0.36"+). This provider replaces that with **observed** precipitation aggregated over the
delineated basin:

* Source: NCEP **MRMS Multi-Sensor QPE**, ``MultiSensor_QPE_72H_Pass2`` — the gauge-corrected
  (Pass2) 72 h radar+gauge accumulation, ~1 km CONUS, published hourly as GRIB2. The 72 h
  window matches the engine's 24-72 h antecedent horizon.
* Domain: aggregated over the upstream watershed polygon with the shared zonal reducer
  (:func:`upstreamwx.grib.zonal.aggregate_over_polygon`), reporting the basin **mean** (the
  physical soil-saturation measure) and **max** (provenance/transparency).

Currency: observed QPE only covers up to ~now, so it is used only when the mission window starts
within the observed horizon (``mrms_future_window_h``); a mission planned days out keeps the
Open-Meteo model QPF (which *can* see a future antecedent window). Graceful degradation is
first-class (NFR-6): any failure here leaves the Open-Meteo point value untouched, and the basin
being unavailable simply means no observed override. This module never imports the engine
(FR-13, §12); it only fills the bundle's antecedent fields.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import requests
import xarray as xr
from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..engine.models import Mission
from ..grib.cache import decode_cached
from ..grib.idx import _HEADERS
from ..grib.zonal import aggregate_over_polygon
from .base import IngestBundle

NAME = "mrms_qpe"

# The gauge-corrected 72 h accumulation product (matches the 24-72 h antecedent horizon).
PRODUCT = "MultiSensor_QPE_72H_Pass2"
_ACCUM_HOURS = 72

# Antecedent-wetness threshold, inches over the window. Shared with the Open-Meteo point proxy
# (``openmeteo._ANTECEDENT_IN``) so both paths flag wetness at the same cut point (§16.1).
ANTECEDENT_IN = 0.25
_MM_PER_IN = 25.4

# MRMS accumulation grids flag no-coverage / missing with negatives (e.g. -3 no radar, -1
# missing); mask *only* negatives so the areal reduction ignores them. A true 0.0 is a valid
# dry cell and must stay in the mean — masking it would bias a large basin's average upward.
_MISSING_MIN = 0.0

# Filenames look like MRMS_MultiSensor_QPE_72H_Pass2_00.00_YYYYMMDD-HHMMSS.grib2.gz
_FILE_RE = re.compile(
    r'href="(MRMS_' + re.escape(PRODUCT) + r'_[\d.]+_(\d{8})-(\d{6})\.grib2\.gz)"'
)


@dataclass(frozen=True)
class QpeAntecedent:
    """Basin-aggregated observed antecedent precipitation (inches over the accumulation window)."""

    mean_in: float | None
    max_in: float | None
    n_cells: int
    valid_time: datetime
    product: str
    fallback_nearest_cell: bool


def _product_url(settings: Settings) -> str:
    return f"{settings.mrms_base_url.rstrip('/')}/{PRODUCT}"


def list_files(
    *, settings: Settings | None = None, timeout: float = 30.0
) -> list[tuple[datetime, str]]:
    """List available ``(valid_time_utc, filename)`` for the product dir, oldest first."""
    settings = settings or get_settings()
    resp = requests.get(_product_url(settings) + "/", headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    out: list[tuple[datetime, str]] = []
    for fname, ymd, hms in _FILE_RE.findall(resp.text):
        try:
            vt = datetime.strptime(ymd + hms, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        out.append((vt, fname))
    out.sort(key=lambda t: t[0])
    return out


def _select_file(
    files: list[tuple[datetime, str]], target: datetime, *, max_age_h: float
) -> tuple[datetime, str] | None:
    """Newest file valid at/just before ``target``; None if none within ``max_age_h`` of it.

    The 72 h product valid at time T covers ``[T-72h, T]``, so we want the newest T not past the
    antecedent window's end (``target`` = mission start, clamped to now upstream). A file older
    than ``max_age_h`` before ``target`` means the observed feed is stale here — degrade to the
    model proxy rather than aggregate hours-old coverage as "current" (NFR-6).
    """
    candidates = [f for f in files if f[0] <= target]
    pick = candidates[-1] if candidates else (files[0] if files else None)
    if pick is None:
        return None
    if abs((target - pick[0]).total_seconds()) > max_age_h * 3600.0:
        return None
    return pick


def _decode_to_inches(path: Path, bbox: tuple[float, float, float, float]) -> xr.DataArray:
    """Decode the MRMS GRIB2 at ``path``, crop to ``bbox``, mask missing, convert mm -> inches.

    ``bbox`` is ``(lat_min, lat_max, lon_min, lon_max)`` in −180..180; MRMS stores longitude as
    0..360, so we crop in that convention. Cropping first keeps the masked reduction off the full
    24.5 M-cell CONUS grid (memory + speed), mirroring the GEFS decode-time crop.
    """
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    da = ds[next(iter(ds.data_vars))]
    lat_min, lat_max, lon_min, lon_max = bbox
    lo0, lo1 = lon_min % 360, lon_max % 360
    # MRMS latitude is stored north-to-south (descending), so slice high->low.
    da = da.sel(latitude=slice(lat_max, lat_min), longitude=slice(lo0, lo1))
    da = da.where(da >= _MISSING_MIN) / _MM_PER_IN
    da.attrs["units"] = "inch"
    return da


def _bbox_with_margin(
    polygon: BaseGeometry, margin_deg: float = 0.1
) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = polygon.bounds  # (lon_min, lat_min, lon_max, lat_max)
    return (miny - margin_deg, maxy + margin_deg, minx - margin_deg, maxx + margin_deg)


def antecedent_over_polygon(
    mission: Mission,
    polygon: BaseGeometry,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    data_dir: Path | None = None,
) -> QpeAntecedent | None:
    """Aggregate observed MRMS 72 h QPE over ``polygon``; None when out of window/unavailable.

    Returns ``None`` (caller keeps the model proxy) when the mission starts beyond the observed
    horizon or no sufficiently-current MRMS file exists. Raising is reserved for genuine fetch
    failures the orchestrator wraps into a degradation note (NFR-6).
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)

    # Observed QPE covers only up to ~now. Clamp the antecedent window's end to now; if the
    # mission starts too far in the future, observed coverage can't represent its antecedent
    # window -> let the model QPF proxy stand.
    start = mission.window_start
    start = start.astimezone(UTC) if start.tzinfo else start.replace(tzinfo=UTC)
    if (start - now).total_seconds() > settings.mrms_future_window_h * 3600.0:
        return None
    target = min(start, now)

    files = list_files(settings=settings)
    pick = _select_file(files, target, max_age_h=settings.mrms_max_age_h)
    if pick is None:
        return None
    valid_time, fname = pick

    data_dir = data_dir or settings.ensure_data_dir()
    dest = Path(data_dir) / "mrms" / fname[:-3]  # strip ".gz"; store the decompressed .grib2
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"{_product_url(settings)}/{fname}"
        resp = requests.get(url, headers=_HEADERS, timeout=(10.0, 120.0))
        resp.raise_for_status()
        raw = gzip.decompress(resp.content)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(dest)

    bbox = _bbox_with_margin(polygon)
    # Decode+crop is serialised on the shared eccodes compute lock and memoised per (file, bbox).
    da = decode_cached(dest, partial(_decode_to_inches, bbox=bbox), key_extra=bbox)
    agg = aggregate_over_polygon(
        da, polygon, field_name="mrms_qpe_72h", threshold=f"{_ACCUM_HOURS}h_accum"
    )
    return QpeAntecedent(
        mean_in=agg.mean_value,
        max_in=agg.max_value,
        n_cells=agg.n_cells,
        valid_time=valid_time,
        product=PRODUCT,
        fallback_nearest_cell=agg.fallback_nearest_cell,
    )


def fetch(
    mission: Mission, bundle: IngestBundle, polygon: BaseGeometry, *, now: datetime | None = None
) -> None:
    """Fill the bundle's antecedent fields from basin-aggregated observed QPE (supersedes model).

    Sets ``antecedent_precip_24_72h`` from the basin **mean** crossing ``ANTECEDENT_IN`` and
    records the numeric provenance. A ``None`` return from :func:`antecedent_over_polygon` (out of
    window / stale feed) leaves the Open-Meteo point value in place — this is the graceful,
    data-quality-first fallback (NFR-6), not an error.
    """
    result = antecedent_over_polygon(mission, polygon, now=now)
    if result is None or result.mean_in is None:
        return
    bundle.antecedent_qpe_mean_in = result.mean_in
    bundle.antecedent_qpe_max_in = result.max_in
    bundle.antecedent_qpe_valid = result.valid_time.isoformat()
    bundle.antecedent_source = "mrms_qpe_72h"
    # Observed basin-mean supersedes the model point proxy for the engine-facing boolean.
    bundle.antecedent_precip_24_72h = result.mean_in >= ANTECEDENT_IN
    bundle.sources_ok[NAME] = True
    note = (
        f"antecedent (observed): MRMS 72 h QPE over the basin — mean {result.mean_in:.2f} in, "
        f"max {result.max_in:.2f} in across {result.n_cells} cell(s), valid "
        f"{result.valid_time:%Y-%m-%d %H:%MZ}"
    )
    if result.fallback_nearest_cell:
        note += " (basin < 1 grid cell; nearest-cell sample)"
    bundle.notes.append(note + ".")
