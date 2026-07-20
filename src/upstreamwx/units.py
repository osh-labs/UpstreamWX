"""Display-only unit localization: US customary ↔ metric (FR-9, §17).

The engine, the threshold YAML, and the ingest bundle are all expressed in the
product's *native* units — temperatures in °F, precip depth in inch, precip rate in
in/hr, wind in mph, CAPE in J/kg; geospatial quantities in km / km². This module
converts those native values to a chosen display system at **render time only**. It
never touches an engine input, a threshold cut point, or a hazard posture, so identical
inputs still produce identical engine output regardless of the display system chosen
(FR-13, FR-20a, NFR-4).

Selecting a system flips a value and its unit label *together* (``temp`` ↔ ``temp_unit``)
so the number and the unit it is shown with can never drift apart. CAPE (J/kg) has no
customary/metric distinction and is deliberately absent here — it is displayed verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

UnitSystem = Literal["us", "metric"]

# Exact conversion factors; display precision is the caller's format string.
_KM_PER_MI = 1.609344
_KM2_TO_SQ_MI = 0.386102
_IN_TO_MM = 25.4


def normalize_units(value: object) -> UnitSystem:
    """Coerce an arbitrary request value to a valid :data:`UnitSystem` (default US).

    Anything that is not exactly ``"metric"`` falls back to ``"us"`` so an unknown or
    missing preference degrades to the product's native system rather than erroring.
    """
    return "metric" if value == "metric" else "us"


@dataclass(frozen=True)
class Units:
    """A display-unit converter for one system (``"us"`` or ``"metric"``).

    Every ``*_unit`` property returns the label string that pairs with the matching
    converter method's output. ``None`` passes through unchanged (an unavailable value
    stays unavailable, NFR-6) so call sites can convert before their own ``n/a`` guard.
    """

    system: UnitSystem = "us"

    @property
    def metric(self) -> bool:
        return self.system == "metric"

    # -- temperature (native °F) --------------------------------------------------
    def temp(self, f: float | None) -> float | None:
        if f is None:
            return None
        return (f - 32.0) * 5.0 / 9.0 if self.metric else f

    @property
    def temp_unit(self) -> str:
        return "°C" if self.metric else "°F"

    # -- precip depth (native inch) -----------------------------------------------
    def depth(self, inch: float | None) -> float | None:
        if inch is None:
            return None
        return inch * _IN_TO_MM if self.metric else inch

    @property
    def depth_unit(self) -> str:
        return "mm" if self.metric else "in"

    @property
    def depth_fmt(self) -> str:
        """Sensible precision per system: whole mm, but tenths of an inch."""
        return "{:.0f}" if self.metric else "{:.1f}"

    # -- precip rate (native in/hr) -----------------------------------------------
    def rate(self, in_per_hr: float | None) -> float | None:
        if in_per_hr is None:
            return None
        return in_per_hr * _IN_TO_MM if self.metric else in_per_hr

    @property
    def rate_unit(self) -> str:
        return "mm/hr" if self.metric else "in/hr"

    # -- wind (native mph) --------------------------------------------------------
    def wind(self, mph: float | None) -> float | None:
        if mph is None:
            return None
        return mph * _KM_PER_MI if self.metric else mph

    @property
    def wind_unit(self) -> str:
        return "km/h" if self.metric else "mph"

    # -- distance (native km) -----------------------------------------------------
    def distance_from_km(self, km: float | None) -> float | None:
        if km is None:
            return None
        return km if self.metric else km / _KM_PER_MI

    @property
    def distance_unit(self) -> str:
        return "km" if self.metric else "mi"

    # -- area (native km²) --------------------------------------------------------
    def area_from_km2(self, km2: float | None) -> float | None:
        if km2 is None:
            return None
        return km2 if self.metric else km2 * _KM2_TO_SQ_MI

    @property
    def area_unit(self) -> str:
        return "km²" if self.metric else "mi²"


def units_for(system: object) -> Units:
    """Build a :class:`Units` for a request value, normalizing unknowns to US."""
    return Units(normalize_units(system))


# -- free-text localization ---------------------------------------------------------
#
# The deterministic engine authors driver/logic strings that embed native units in prose
# (e.g. "Heat index 95 °F", "Caution 80–90 °F", "≥0.5 in/1 h", "Wind 12 mph"). Those strings
# are engine output — converting them at the source would make the engine's text depend on a
# display parameter (NFR-4), so they are localized *here*, at render time, purely for display.
# Patterns are deliberately narrow (a number, optionally a dash-range, immediately before an
# unambiguous unit token) so ordinary prose — the preposition "in", a bare percentage — is
# never touched. Range forms are handled before singles so "80–90 °F" converts both bounds.
_NUM = r"-?\d+(?:\.\d+)?"
_DASH = r"[–\-]"  # en-dash (used in ranges like "80–90") or hyphen
_F_RANGE = re.compile(rf"({_NUM})\s*{_DASH}\s*({_NUM})\s*°F")
_F_ONE = re.compile(rf"({_NUM})\s*°F")
_MPH = re.compile(rf"({_NUM})\s*mph\b")
_IN_HR = re.compile(rf"({_NUM})\s*in/hr\b")
_IN_ACCUM = re.compile(rf"({_NUM})\s*in/(\d+)\s*h\b")  # accumulation "0.5 in/1 h"


def localize_units_text(text: str, u: Units) -> str:
    """Convert native-unit tokens in engine-authored prose to the display system (display-only).

    A no-op for US (returns ``text`` unchanged, so US output is byte-identical, NFR-4). For
    metric, rewrites °F→°C, mph→km/h, in/hr→mm/hr, and inch accumulations (``in/N h``)→mm,
    leaving percentages, J/kg, and the numbers' surrounding words intact.
    """
    if not u.metric or not text:
        return text

    def c(f: str) -> str:
        return f"{u.temp(float(f)):.0f}"

    text = _F_RANGE.sub(lambda m: f"{c(m.group(1))}–{c(m.group(2))} °C", text)
    text = _F_ONE.sub(lambda m: f"{c(m.group(1))} °C", text)
    text = _MPH.sub(lambda m: f"{u.wind(float(m.group(1))):.0f} km/h", text)
    text = _IN_HR.sub(lambda m: f"{u.depth(float(m.group(1))):.0f} mm/hr", text)
    text = _IN_ACCUM.sub(lambda m: f"{u.depth(float(m.group(1))):.0f} mm/{m.group(2)} h", text)
    return text
