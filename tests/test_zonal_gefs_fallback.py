"""Nearest-cell fallback on the GEFS regular 1D lat/lon grid (regression).

:func:`aggregate_over_polygon` falls back to the nearest cell when a polygon is smaller
than a grid cell (``n_cells == 0``). The fallback unraveled a flat argmin via
``lat.shape``, which assumes the **2D** curvilinear coordinates the SREF/HREF/REFS Lambert
grids carry. GEFS is a **regular 1D** lat/lon mesh, so that unpack raised
``ValueError: not enough values to unpack`` and sank the entire GEFS source for any domain
too small to hold a 0.25° cell centre (a 24 km Lightning-Area-of-Concern disk, a clipped
Radius of Concern). These tests pin both grid shapes through the fallback.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from shapely.geometry import box

from upstreamwx.grib.zonal import aggregate_over_polygon


def _gefs_like_grid() -> xr.DataArray:
    """A small GEFS-style regular grid: 1D descending latitude, 0.25° spacing."""
    lats = np.arange(40.0, 36.0, -0.25)
    lons = np.arange(-112.0, -108.0, 0.25)
    data = np.arange(lats.size * lons.size, dtype=float).reshape(lats.size, lons.size)
    return xr.DataArray(
        data,
        dims=("latitude", "longitude"),
        coords={"latitude": lats, "longitude": lons},
        name="APCP",
    )


def test_subcell_polygon_falls_back_to_nearest_cell_on_1d_grid() -> None:
    """A polygon smaller than a 0.25° cell yields the nearest-cell value, not a ValueError."""
    da = _gefs_like_grid()
    tiny = box(-110.06, 38.06, -110.04, 38.08)  # ~2 km, between cell centres

    agg = aggregate_over_polygon(da, tiny, field_name="APCP", threshold="max")

    assert agg.fallback_nearest_cell is True
    assert agg.n_cells == 0
    lats = da["latitude"].values
    lons = da["longitude"].values
    iy = int(np.argmin(np.abs(lats - 38.07)))
    ix = int(np.argmin(np.abs(lons - (-110.05))))
    assert agg.max_value == da.values[iy, ix]
    assert agg.mean_value == da.values[iy, ix]


def test_polygon_with_cells_uses_areal_path_on_1d_grid() -> None:
    """A polygon covering several cells takes the masked areal path (no fallback)."""
    da = _gefs_like_grid()
    big = box(-111.5, 37.0, -109.0, 39.0)  # spans many 0.25° cells

    agg = aggregate_over_polygon(da, big, field_name="APCP", threshold="max")

    assert agg.fallback_nearest_cell is False
    assert agg.n_cells > 0
