"""Verify-against-NWS source links for the SITREP "SOURCES" section (FR-26, FR-40).

The briefing is reference-only: every render carries links back to the authoritative
NWS products and the model source so a trip leader can verify (PRD §15, Appendix A).
The point links are built here; the model-source links reuse the feed bases already
defined by the GEFS/REFS source modules so there is a single source of truth. (GEFS and
REFS replace SREF and HREF after the 2026-08-31 EOL, NWS SCN 26-47.)
"""

from __future__ import annotations

from dataclasses import dataclass

from ..gefs.sources import NOMADS_BASE as GEFS_NOMADS_BASE
from ..refs.sources import AWS_BASE as REFS_AWS_BASE


@dataclass(frozen=True)
class SourceLinks:
    """The verify-against links shown on a briefing."""

    active_alerts: str
    nws_point_forecast: str
    gefs_model: str
    refs_model: str | None  # only populated when REFS fed the briefing


def build_source_links(lat: float, lon: float, *, used_refs: bool = False) -> SourceLinks:
    """Build the verify-against-NWS links for a mission point.

    ``lat``/``lon`` format the active-alerts and point-forecast links; ``used_refs``
    gates the REFS model link so it appears only when the same-day supplement was in
    range for this briefing (FR-7a).
    """
    return SourceLinks(
        active_alerts=f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}",
        nws_point_forecast=f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lon:.4f}",
        gefs_model=GEFS_NOMADS_BASE,
        refs_model=REFS_AWS_BASE if used_refs else None,
    )
