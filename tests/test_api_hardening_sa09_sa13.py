"""Host-header validation (SA-09) and healthcheck-URL redaction (SA-13).

Both are hardening for the public beta: TrustedHostMiddleware rejects an unexpected Host on
a direct hit to the loopback uvicorn (defence behind nginx's server_name), and the scheduler's
best-effort monitoring ping must never place its secret-bearing URL in the log.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from upstreamwx.api.app import _trusted_host_allowlist
from upstreamwx.api.scheduler import _redact_ping_url


class _Settings:
    """Minimal stand-in exposing just the setting the helper reads."""

    def __init__(self, hosts):
        self.api_trusted_hosts = hosts


# -- SA-09: _trusted_host_allowlist -----------------------------------------------------------

def test_trusted_hosts_off_by_default():
    assert _trusted_host_allowlist(_Settings(None)) is None
    assert _trusted_host_allowlist(_Settings([])) is None


def test_trusted_hosts_appends_loopback():
    allowed = _trusted_host_allowlist(_Settings(["app.upstreamwx.com"]))
    assert allowed is not None
    assert "app.upstreamwx.com" in allowed
    # Loopback names always present so the direct /v1/health probe + local uvicorn keep working.
    for lo in ("127.0.0.1", "localhost", "::1"):
        assert lo in allowed
    # No duplicates even if the operator lists a loopback name themselves.
    dup = _trusted_host_allowlist(_Settings(["localhost", "app.upstreamwx.com"]))
    assert dup.count("localhost") == 1


def test_trusted_host_middleware_rejects_unknown_host():
    """Wired the way app.py wires it, a bad Host is 400 and an allowed/loopback Host is 200."""
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=_trusted_host_allowlist(_Settings(["good.example"]))
    )
    client = TestClient(app)
    assert client.get("/ping", headers={"host": "good.example"}).status_code == 200
    assert client.get("/ping", headers={"host": "127.0.0.1:8000"}).status_code == 200  # loopback ok
    assert client.get("/ping", headers={"host": "evil.example"}).status_code == 400


# -- SA-13: _redact_ping_url ------------------------------------------------------------------

def test_redact_ping_url_strips_secret_path():
    secret = "https://hc-ping.com/9d0f5e2a-secret-uuid-token/start"
    red = _redact_ping_url(secret)
    assert "9d0f5e2a-secret-uuid-token" not in red
    assert red == "https://hc-ping.com/<redacted>"


def test_redact_ping_url_handles_garbage():
    assert _redact_ping_url("not a url") == "<redacted>"
    assert _redact_ping_url("") == "<redacted>"
