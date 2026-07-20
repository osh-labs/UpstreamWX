"""Tests for the display-only unit localization layer (:mod:`upstreamwx.units`).

The converter and text localizer only affect *display*: the US path must be a byte-for-byte
no-op (NFR-4) and metric must convert values and unit labels together, never touching an
engine input or a threshold. These are pure-function tests; the render/structured integration
lives in test_sitrep_render.py and test_structured.py.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from upstreamwx.units import Units, localize_units_text, normalize_units, units_for


def test_normalize_units_defaults_unknown_to_us() -> None:
    assert normalize_units("metric") == "metric"
    assert normalize_units("us") == "us"
    for junk in ("imperial", "", None, 1, "METRIC"):
        assert normalize_units(junk) == "us"


def test_us_converter_is_identity() -> None:
    u = units_for("us")
    assert not u.metric
    assert u.temp(72.0) == 72.0
    assert u.wind(12.0) == 12.0
    assert u.depth(0.6) == 0.6
    assert u.rate(0.5) == 0.5
    assert u.distance_from_km(16.09344) == pytest.approx(10.0)  # km -> mi in US
    assert u.area_from_km2(12.95) == pytest.approx(5.0, abs=0.01)
    assert (u.temp_unit, u.wind_unit, u.depth_unit, u.rate_unit) == ("°F", "mph", "in", "in/hr")
    assert (u.distance_unit, u.area_unit) == ("mi", "mi²")


def test_metric_converter_values_and_labels() -> None:
    u = units_for("metric")
    assert u.metric
    assert u.temp(32.0) == pytest.approx(0.0)
    assert u.temp(212.0) == pytest.approx(100.0)
    assert u.temp(95.0) == pytest.approx(35.0)
    assert u.wind(10.0) == pytest.approx(16.09344)
    assert u.depth(1.0) == pytest.approx(25.4)
    assert u.rate(0.5) == pytest.approx(12.7)
    assert u.distance_from_km(20.0) == 20.0  # km stays km in metric
    assert u.area_from_km2(12.95) == 12.95
    assert (u.temp_unit, u.wind_unit, u.depth_unit, u.rate_unit) == ("°C", "km/h", "mm", "mm/hr")
    assert (u.distance_unit, u.area_unit) == ("km", "km²")


def test_none_passes_through_both_systems() -> None:
    for u in (units_for("us"), units_for("metric")):
        assert u.temp(None) is None
        assert u.wind(None) is None
        assert u.depth(None) is None
        assert u.rate(None) is None
        assert u.distance_from_km(None) is None
        assert u.area_from_km2(None) is None


def test_localize_text_us_is_noop() -> None:
    u = units_for("us")
    text = "Heat index 95 °F → Extreme; Caution 80–90 °F; Wind 12 mph; ≥0.5 in/1 h; 0.5 in/hr"
    assert localize_units_text(text, u) == text
    assert localize_units_text("", u) == ""


def test_localize_text_metric_single_and_range() -> None:
    u = units_for("metric")
    assert localize_units_text("Heat index 95 °F → Extreme Caution", u) == (
        "Heat index 35 °C → Extreme Caution"
    )
    # Both bounds of a range convert (regression: a naive regex would only touch the second).
    assert localize_units_text("Caution 80–90 °F", u) == "Caution 27–32 °C"
    assert localize_units_text("Apparent temperature 32 °F", u) == "Apparent temperature 0 °C"


def test_localize_text_metric_wind_and_precip() -> None:
    u = units_for("metric")
    assert localize_units_text("Wind 12 mph", u) == "Wind 19 km/h"
    assert localize_units_text("rate 0.5 in/hr", u) == "rate 13 mm/hr"
    assert localize_units_text("REFS NEP(≥0.5 in/1 h)", u) == "REFS NEP(≥13 mm/1 h)"


def test_localize_text_leaves_percent_jkg_and_prose_untouched() -> None:
    u = units_for("metric")
    # No physical-unit token → unchanged: percentages, CAPE, and the preposition "in".
    text = "GEFS P(precip) 65% ≥ 60%; CAPE 2800 J/kg; rain falling miles upstream in the basin"
    assert localize_units_text(text, u) == text


def test_units_dataclass_is_frozen() -> None:
    u = Units("metric")
    with pytest.raises(FrozenInstanceError):
        u.system = "us"  # type: ignore[misc]
