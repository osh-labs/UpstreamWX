"""Per-principal + global fair-use budget tests, and SA-03 registration gating (SA-01).

Budgets only apply when the access gate is on; they charge WORK, never cache hits. The
per-principal windows enforce fairness (429), the global windows are absolute ceilings (503),
and scheduled-refresh registration is capped per principal so one client can't fill the shared
active-mission registry (SA-03). All offline — no network, no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from upstreamwx.api.app import app, service
from upstreamwx.api.models import MissionSpec
from upstreamwx.api.service import BriefingService

FIXTURES = Path(__file__).parent / "fixtures" / "sitrep"
SAMPLE_INPUTS = yaml.safe_load((FIXTURES / "sample_inputs.yaml").read_text())["inputs"]
_SECRET = "test-session-secret-0123456789abcdef"


def _base(**overrides) -> dict:
    base = dict(
        lat=37.0192,
        lon=-111.9889,
        activity="canyon",
        start="2026-06-20T08:00",
        end="2026-06-20T18:00",
        name="Buckskin Gulch",
        slot=True,
        frame=False,
        inputs=SAMPLE_INPUTS,
    )
    base.update(overrides)
    return base


@pytest.fixture
def authed_client(monkeypatch):
    """A TestClient with the gate on and a minted session cookie already attached."""
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_WARM", "0")
    monkeypatch.setenv("UPSTREAMWX_API_AUTH_ENABLED", "1")
    monkeypatch.setenv("UPSTREAMWX_SESSION_SECRET", _SECRET)
    monkeypatch.setenv("UPSTREAMWX_SESSION_COOKIE_SECURE", "0")
    service.cache.clear()
    with TestClient(app) as c:
        assert c.post("/v1/session").status_code == 200
        yield c
    service.cache.clear()


def test_per_principal_cold_budget_returns_429(authed_client, monkeypatch):
    monkeypatch.setenv("UPSTREAMWX_BUDGET_COLD_PER_PRINCIPAL_PER_HOUR", "2")
    codes = [
        authed_client.post("/v1/briefing", json=_base(lat=37.0 + i * 0.01)).status_code
        for i in range(3)
    ]
    assert codes[:2] == [200, 200]
    assert codes[2] == 429  # third distinct cold miss is over the per-principal budget


def test_global_cold_ceiling_returns_503(authed_client, monkeypatch):
    monkeypatch.setenv("UPSTREAMWX_BUDGET_GLOBAL_COLD_PER_HOUR", "1")
    monkeypatch.setenv("UPSTREAMWX_BUDGET_COLD_PER_PRINCIPAL_PER_HOUR", "100")
    first = authed_client.post("/v1/briefing", json=_base(lat=37.10)).status_code
    second = authed_client.post("/v1/briefing", json=_base(lat=37.20)).status_code
    assert first == 200
    assert second == 503  # global ceiling (circuit breaker), independent of the principal budget


def test_cache_hit_is_not_charged(authed_client, monkeypatch):
    """Re-requesting the same mission is a free cache hit and does not draw on the budget."""
    monkeypatch.setenv("UPSTREAMWX_BUDGET_COLD_PER_PRINCIPAL_PER_HOUR", "1")
    a1 = authed_client.post("/v1/briefing", json=_base(lat=37.30)).status_code  # miss → charged
    a2 = authed_client.post("/v1/briefing", json=_base(lat=37.30)).status_code  # hit → free
    b = authed_client.post("/v1/briefing", json=_base(lat=37.40)).status_code  # 2nd miss → over
    assert (a1, a2, b) == (200, 200, 429)


# -- SA-03: scheduled-refresh registration is capped per principal (service level) ----------
def _offline_service(monkeypatch) -> BriefingService:
    from upstreamwx.engine.models import HazardInputs
    from upstreamwx.sitrep import generate as generate_mod

    real = generate_mod.generate_briefing

    def offline(mission, *, inputs=None, frame=None, generated_at=None, cycle=None):
        return real(
            mission, inputs=inputs or HazardInputs(), frame=False, generated_at=generated_at
        )

    monkeypatch.setattr("upstreamwx.api.service.generate_briefing", offline)
    svc = BriefingService()
    monkeypatch.setattr(svc, "_cycle_token", lambda now: "2026-06-19T06Z")
    return svc


def test_active_registration_capped_per_principal(monkeypatch):
    monkeypatch.setenv("UPSTREAMWX_API_AUTH_ENABLED", "1")
    monkeypatch.setenv("UPSTREAMWX_SESSION_SECRET", _SECRET)
    monkeypatch.setenv("UPSTREAMWX_BUDGET_ACTIVE_PER_PRINCIPAL", "2")
    svc = _offline_service(monkeypatch)
    now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)

    def live(lat: float) -> MissionSpec:
        return MissionSpec(
            **_base(lat=lat, start="2026-06-20T08:00", end="2026-06-20T18:00", inputs=None)
        )

    # Principal A registers three distinct live missions but only 2 are kept for refresh.
    for lat in (37.01, 37.11, 37.21):
        svc.get_briefing(live(lat), now=now, principal_pid="A")
    assert svc.active_count == 2

    # A different principal is unaffected by A's quota.
    svc.get_briefing(live(37.31), now=now, principal_pid="B")
    assert svc.active_count == 3


def test_registration_uncapped_when_gate_off(monkeypatch):
    """With the gate off, principal quotas don't apply — behaviour is unchanged (regression)."""
    monkeypatch.setenv("UPSTREAMWX_API_AUTH_ENABLED", "0")
    monkeypatch.setenv("UPSTREAMWX_BUDGET_ACTIVE_PER_PRINCIPAL", "1")
    svc = _offline_service(monkeypatch)
    now = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    for lat in (37.01, 37.11, 37.21):
        spec = MissionSpec(
            **_base(lat=lat, start="2026-06-20T08:00", end="2026-06-20T18:00", inputs=None)
        )
        svc.get_briefing(spec, now=now, principal_pid=None)
    assert svc.active_count == 3
