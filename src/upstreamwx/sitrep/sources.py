"""Verify-against-NWS source links for the SITREP "SOURCES" section (FR-26, FR-40).

The briefing is reference-only: every render carries links back to the authoritative
NWS products and the model source so a trip leader can verify (PRD §15, Appendix A).
The point links are built here; the model-source links reuse the NOMADS bases already
defined by the SREF/HREF source modules so there is a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..href.sources import NOMADS_BASE as HREF_NOMADS_BASE
from ..sref.sources import NOMADS_BASE as SREF_NOMADS_BASE


@dataclass(frozen=True)
class SourceLinks:
    """The verify-against links shown on a briefing."""

    active_alerts: str
    nws_point_forecast: str
    sref_model: str
    href_model: str | None  # only populated when HREF fed the briefing


def build_source_links(lat: float, lon: float, *, used_href: bool = False) -> SourceLinks:
    """Build the verify-against-NWS links for a mission point.

    ``lat``/``lon`` format the active-alerts and point-forecast links; ``used_href``
    gates the HREF model link so it appears only when the same-day supplement was in
    range for this briefing (FR-7a).
    """
    return SourceLinks(
        active_alerts=f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}",
        nws_point_forecast=f"https://forecast.weather.gov/MapClick.php?lat={lat:.4f}&lon={lon:.4f}",
        sref_model=SREF_NOMADS_BASE,
        href_model=HREF_NOMADS_BASE if used_href else None,
    )
