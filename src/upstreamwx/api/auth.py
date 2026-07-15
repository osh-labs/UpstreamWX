"""Anonymous fair-use session tokens for the public-release access gate (SA-01).

Establishes a per-CLIENT principal — a random opaque id carried in a stateless,
HMAC-SHA256-signed token — so cost/abuse budgets attach to an app-issued identity
instead of a bare IP (the audit's finding: "IP-only throttling is weak identity and is
readily shared, rotated, or bypassed"). There is **no login and no personal data**: the
token authenticates a *client*, not a person, purely for fair-use accounting.

Stateless by design (no server-side session table): the signature alone proves
authenticity, so the gate works across a process restart and would work across workers
unchanged. Only the budget *counters* (:mod:`upstreamwx.api.budget`) are process-local —
the same "in-process now, shared store at M0.1.1" boundary the briefing cache documents.

Transport is an HttpOnly / Secure / SameSite=Lax cookie set by ``POST /v1/session``:
- **HttpOnly** keeps the token unreadable by page JavaScript, so a compromised third-party
  script (SA-05) cannot exfiltrate it;
- **SameSite=Lax** is the CSRF control — the browser will not attach the cookie to a
  cross-site POST, and the sensitive endpoints are all same-origin JSON POSTs (no CORS is
  granted), so a forged cross-site request carries no session;
- **Secure** requires HTTPS (a hard deployment prerequisite, SA-09).
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets as _secrets
import time
from dataclasses import dataclass
from hashlib import sha256

from fastapi import HTTPException, Request
from fastapi.responses import Response

from ..config import Settings, get_settings

# Cookie name the PWA never reads (HttpOnly); the app mints and verifies it.
SESSION_COOKIE = "uwx_session"
# Token layout version, so a future format change can be distinguished from a forgery.
_TOKEN_VERSION = 1


@dataclass(frozen=True)
class Principal:
    """The authenticated client identity a request acts as (SA-01).

    ``pid`` is an opaque random id (no personal meaning); ``tier`` distinguishes the
    anonymous public tier from the synthetic principal used when the gate is disabled.
    """

    pid: str
    tier: str = "anon"


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(secret: str, payload_b64: str) -> str:
    """HMAC-SHA256 of the encoded payload, itself base64url-encoded."""
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), sha256).digest()
    return _b64u_encode(mac)


def mint(secret: str, *, ttl: int, tier: str = "anon", now: float | None = None) -> str:
    """Mint a signed anonymous session token valid for ``ttl`` seconds.

    The payload is a fresh random ``pid`` plus issued-at/expiry; the token is
    ``<b64url(payload)>.<b64url(hmac)>``. Stateless — nothing is stored server-side.
    """
    now = time.time() if now is None else now
    payload = {
        "v": _TOKEN_VERSION,
        "pid": _secrets.token_hex(16),
        "iat": int(now),
        "exp": int(now) + int(ttl),
        "tier": tier,
    }
    payload_b64 = _b64u_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload_b64}.{_sign(secret, payload_b64)}"


def verify(token: str | None, secrets: list[str], *, now: float | None = None) -> Principal | None:
    """Return the :class:`Principal` for a valid, unexpired token, else ``None``.

    Accepts any of ``secrets`` (current + previous) so the signing key can be rotated with
    zero session loss. Signature is compared in constant time; a tampered, malformed, or
    expired token verifies as ``None`` (the middleware maps that to 401).
    """
    if not token or "." not in token:
        return None
    payload_b64, sig = token.rsplit(".", 1)
    if not any(sec and hmac.compare_digest(sig, _sign(sec, payload_b64)) for sec in secrets):
        return None
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != _TOKEN_VERSION:
        return None
    exp = payload.get("exp")
    now = time.time() if now is None else now
    if not isinstance(exp, int) or now >= exp:
        return None
    pid = payload.get("pid")
    if not isinstance(pid, str) or not pid:
        return None
    return Principal(pid=pid, tier=str(payload.get("tier") or "anon"))


def auth_active(settings: Settings) -> bool:
    """Whether the access gate is actually enforcing (SA-01).

    Secret-gated activation: the gate is on by default (``api_auth_enabled``) but only ENFORCES
    when a signing secret is present, so the secretless contexts (dev, CLI, the offline test
    suite, the tailnet beta) run open without a crash, while the public host activates the gate
    simply by setting ``UPSTREAMWX_SESSION_SECRET``. The single source of truth used by the
    middleware, the ``require_session`` dependency, budget charging, and refresh registration.
    """
    return bool(settings.api_auth_enabled and settings.session_secret)


def session_secrets(settings: Settings) -> list[str]:
    """The accepted signing secrets (current first, then the verify-only previous)."""
    return [s for s in (settings.session_secret, settings.session_secret_prev) if s]


def read_token(request: Request) -> str | None:
    """Extract the session token from the cookie, or a ``Bearer`` header as a fallback.

    Browsers use the HttpOnly cookie; the header path lets a non-browser API client (a
    future named-token tier) present a token without a cookie jar.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return None


def set_session_cookie(response: Response, token: str, *, ttl: int, secure: bool) -> None:
    """Attach the session token as an HttpOnly / SameSite=Lax cookie (see module docstring)."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=ttl,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def require_session(request: Request) -> Principal:
    """FastAPI dependency: the authenticated :class:`Principal` for a gated endpoint.

    When the gate is inactive (disabled, or no secret configured — the tailnet beta and the
    offline tests) it returns a synthetic open-tier principal so handlers and budgets are no-ops.
    When active it returns the principal the :class:`SessionMiddleware` already verified and
    stashed on the ASGI scope; a missing one is a 401 (defence in depth behind the middleware,
    which already blocks unauthenticated requests to gated paths).
    """
    if not auth_active(get_settings()):
        return Principal(pid="anon", tier="open")
    principal = request.scope.get("uwx_principal")
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=401, detail="A session is required — reload the app.")
    return principal
