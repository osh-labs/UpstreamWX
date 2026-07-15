"""Anonymous-session access-gate tests (SA-01).

Covers the finding's three acceptance criteria plus the token primitives:

* an unauthenticated request to each expensive /v1 endpoint is denied (401);
* an authorised client (a minted session cookie) completes the normal flow;
* the gate is enforced IN THE APP — a TestClient talks straight to the ASGI app with no
  nginx, so a pass here proves port-8000-direct cannot bypass the control.

All offline — no network, no LLM. The gate defaults OFF, so these tests turn it on via env;
the rest of the suite is unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from upstreamwx.api import auth
from upstreamwx.api.app import app, service

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
def auth_env(monkeypatch):
    """Turn the access gate ON with a known secret and a dev (insecure) cookie."""
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_WARM", "0")
    monkeypatch.setenv("UPSTREAMWX_API_AUTH_ENABLED", "1")
    monkeypatch.setenv("UPSTREAMWX_SESSION_SECRET", _SECRET)
    monkeypatch.setenv("UPSTREAMWX_SESSION_COOKIE_SECURE", "0")  # http TestClient keeps the cookie
    service.cache.clear()
    yield
    service.cache.clear()


@pytest.fixture
def client(auth_env):
    with TestClient(app) as c:
        yield c


def _mint(client: TestClient) -> None:
    resp = client.post("/v1/session")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "auth": True}
    assert auth.SESSION_COOKIE in client.cookies


# -- Acceptance #1 + #3: unauthenticated requests to gated endpoints are denied ------------
def test_gated_endpoints_denied_without_session(client):
    for method_path, body in [
        ("/v1/briefing", _base()),
        ("/v1/briefing/frame", _base()),
        ("/v1/briefing/pdf", {}),
        ("/v1/watershed/warm", {"lat": 37.0, "lon": -111.9}),
    ]:
        resp = client.post(method_path, json=body)
        assert resp.status_code == 401, method_path
        assert "session is required" in resp.text.lower()


def test_exempt_endpoints_reachable_without_session(client):
    """Health and the mint endpoint must not be gated, or the boot flow can't start."""
    assert client.get("/v1/health").status_code == 200
    assert client.post("/v1/session").status_code == 200


def test_unknown_v1_path_is_gated_not_routed(client):
    """Fail-closed by path: a new /v1 route is denied even before routing (allowlist)."""
    assert client.post("/v1/anything-new").status_code == 401


# -- Acceptance #2: an authorised client completes the normal flow -------------------------
def test_authorised_flow_completes(client):
    _mint(client)
    # briefing (offline inputs) succeeds
    r_brief = client.post("/v1/briefing", json=_base())
    assert r_brief.status_code == 200
    # frame streams (204 with no ANTHROPIC key configured) — the point is it's not 401
    assert client.post("/v1/briefing/frame", json=_base()).status_code in (204, 200)
    # warm is accepted (202; noop with the pool disabled in tests)
    assert client.post("/v1/watershed/warm", json={"lat": 37.0, "lon": -111.9}).status_code == 202
    # pdf passes the gate: a bad body is a 422 (validation), never a 401 (auth)
    assert client.post("/v1/briefing/pdf", json={}).status_code == 422


# -- token integrity -----------------------------------------------------------------------
def test_tampered_cookie_rejected(client):
    _mint(client)
    tok = client.cookies[auth.SESSION_COOKIE]
    client.cookies.set(auth.SESSION_COOKIE, tok[:-3] + "xxx")  # corrupt the signature tail
    assert client.post("/v1/briefing", json=_base()).status_code == 401


def test_foreign_secret_cookie_rejected(client):
    """A token signed with a different secret must not verify."""
    forged = auth.mint("some-other-secret", ttl=3600)
    client.cookies.set(auth.SESSION_COOKIE, forged)
    assert client.post("/v1/briefing", json=_base()).status_code == 401


def test_expired_token_rejected(client):
    _mint(client)
    expired = auth.mint(_SECRET, ttl=-10)  # already past its exp
    client.cookies.set(auth.SESSION_COOKIE, expired)
    assert client.post("/v1/briefing", json=_base()).status_code == 401


# -- session minting hardening -------------------------------------------------------------
def test_mint_is_rate_limited(client):
    """Freely mintable tokens are the weak point — minting is capped per IP (default 5/min)."""
    codes = [client.post("/v1/session").status_code for _ in range(8)]
    assert 429 in codes
    assert codes.count(200) <= 5


# -- unit: mint / verify / rotation --------------------------------------------------------
def test_verify_roundtrip_and_rejections():
    tok = auth.mint(_SECRET, ttl=3600)
    p = auth.verify(tok, [_SECRET])
    assert p is not None and p.pid and p.tier == "anon"
    assert auth.verify(tok, ["wrong"]) is None
    assert auth.verify(tok[:-1] + ("a" if tok[-1] != "a" else "b"), [_SECRET]) is None
    assert auth.verify(auth.mint(_SECRET, ttl=-5), [_SECRET]) is None
    assert auth.verify(None, [_SECRET]) is None
    assert auth.verify("not-a-token", [_SECRET]) is None
    # Two mints give two distinct principals (a fresh random pid each time).
    assert auth.verify(auth.mint(_SECRET, ttl=60), [_SECRET]).pid != p.pid


def test_secret_rotation_accepts_previous():
    """A token signed with the previous secret still verifies during a rotation window."""
    old = auth.mint("old-secret", ttl=3600)
    assert auth.verify(old, ["new-secret", "old-secret"]) is not None  # current + prev accepted
    assert auth.verify(old, ["new-secret"]) is None  # once prev is dropped, it stops verifying


# -- fail-closed: gate on with no secret refuses to start ----------------------------------
def test_fail_closed_without_secret(monkeypatch):
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_WARM", "0")
    monkeypatch.setenv("UPSTREAMWX_API_AUTH_ENABLED", "1")
    monkeypatch.delenv("UPSTREAMWX_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        with TestClient(app):
            pass


# -- regression: gate OFF (default) needs no session ---------------------------------------
def test_gate_off_needs_no_session(monkeypatch):
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("UPSTREAMWX_API_ENABLE_WARM", "0")
    monkeypatch.setenv("UPSTREAMWX_API_AUTH_ENABLED", "0")
    service.cache.clear()
    with TestClient(app) as c:
        assert c.post("/v1/briefing", json=_base()).status_code == 200
    service.cache.clear()
