"""Cross-request cache isolation for mission metadata (SA-04).

The response cache key must fold in every user-supplied mission field that reaches the
response, so two differently-labelled missions at the same conditions can never be served
each other's mission name or presentation (a cross-user disclosure / cache-poisoning
vector). These tests drive the service end to end through the offline (network-free)
generation path and assert the served briefing always carries the *current* request's
metadata.
"""

from __future__ import annotations

from datetime import UTC, datetime

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

    def offline(mission, *, inputs=None, frame=None, generated_at=None, cycle=None):
        return real(mission, inputs=inputs or HazardInputs(), frame=False,
                    generated_at=generated_at)

    monkeypatch.setattr("upstreamwx.api.service.generate_briefing", offline)
    service = BriefingService()
    # Deterministic, network-free cache token so A and B share a cycle (a conditions-only
    # re-request is a hit) without probing NOMADS.
    monkeypatch.setattr(service, "_cycle_token", lambda now: cycle_key(now))
    return service


def _spec(**overrides) -> MissionSpec:
    base = dict(
        lat=37.0192, lon=-111.9889, activity="canyon",
        start="2026-06-20T08:00", end="2026-06-20T18:00", frame=False,
    )
    base.update(overrides)
    return MissionSpec(**base)


def test_differing_metadata_never_returns_other_missions_name(monkeypatch):
    """Acceptance 1: a request differing only in name gets its OWN name, never the first's."""
    service = _offline_service(monkeypatch)
    now = _utc(2026, 6, 19, 12)

    first = service.get_briefing(_spec(name="Secret Slot Canyon"), now=now)
    assert first.cached is False
    assert first.mission.name == "Secret Slot Canyon"
    assert "Secret Slot Canyon" in first.markdown

    # Same conditions, different (default) name -> a cache MISS -> the current request's name.
    second = service.get_briefing(_spec(name="mission"), now=now)
    assert second.cached is False  # metadata differs -> distinct key -> not the first's entry
    assert second.mission.name == "mission"
    assert "Secret Slot Canyon" not in second.markdown  # no leak of the first mission's label


def test_party_size_and_route_note_key_apart(monkeypatch):
    """party_size / route_note also key apart (defensive against a future render of them)."""
    service = _offline_service(monkeypatch)
    now = _utc(2026, 6, 19, 12)

    service.get_briefing(_spec(name="trip", party_size=2), now=now)
    other = service.get_briefing(_spec(name="trip", party_size=9), now=now)
    assert other.cached is False  # party_size is part of identity

    service.get_briefing(_spec(name="trip", route_note="via the narrows"), now=now)
    diff_note = service.get_briefing(_spec(name="trip", route_note="via the escape"), now=now)
    assert diff_note.cached is False  # route_note is part of identity


def test_identical_request_still_hits(monkeypatch):
    """Acceptance 2: a byte-identical (conditions + metadata) re-request is a free cache hit."""
    service = _offline_service(monkeypatch)
    now = _utc(2026, 6, 19, 12)

    service.get_briefing(_spec(name="Buckskin"), now=now)
    again = service.get_briefing(_spec(name="Buckskin"), now=now)
    assert again.cached is True
    assert again.mission.name == "Buckskin"


def test_conditions_change_misses_and_renders_current(monkeypatch):
    """A conditions-only change (location) misses and renders the current request."""
    service = _offline_service(monkeypatch)
    now = _utc(2026, 6, 19, 12)

    service.get_briefing(_spec(name="A", lat=37.0192), now=now)
    moved = service.get_briefing(_spec(name="A", lat=38.0), now=now)
    assert moved.cached is False
    assert abs(moved.mission.lat - 38.0) < 1e-6


def test_key_is_metadata_sensitive_but_stable():
    """Unit: the key changes with metadata and is stable for an identical mission."""
    m = _spec(name="orig").to_mission()
    assert mission_cache_key(m) == mission_cache_key(_spec(name="orig").to_mission())
    assert mission_cache_key(m) != mission_cache_key(_spec(name="orig ").to_mission())  # whitespace
    assert mission_cache_key(m) != mission_cache_key(_spec(name="other").to_mission())
