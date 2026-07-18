"""PDF-endpoint input hardening (SA-08).

The `/v1/briefing/pdf` endpoint renders *client-supplied* `BriefingResponse` JSON in headless
Chromium, so the payload is treated as hostile. These tests cover the two in-repo SA-08 fixes:
(1) generous-but-real bounds on the broad response fields, so a hostile payload can't carry
huge lists/strings into the render, and (2) a streaming body reject on the PDF path so a chunked
upload can't buffer unbounded before the size check. (The residual — restoring Chromium's native
sandbox — is host-dependent and tracked for the deploy pass.)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from upstreamwx.api.app import _MaxBodySizeMiddleware, app, service
from upstreamwx.api.models import BriefingResponse

_SAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "frontend" / "data" / "sample-briefing.json").read_text()
)


@pytest.fixture
def client():
    os.environ["UPSTREAMWX_API_ENABLE_SCHEDULER"] = "0"
    service.cache.clear()
    with TestClient(app) as c:
        yield c
    service.cache.clear()


# -- SA-08 #2: nested response-field bounds --------------------------------------------------

def test_legitimate_contract_still_validates():
    """The frozen server-built contract must still validate — caps are above real output."""
    BriefingResponse.model_validate(_SAMPLE)


@pytest.mark.parametrize(
    "field,value",
    [
        ("warnings", ["x"] * 1000),                                   # list cardinality
        ("warnings", ["x" * 5000]),                                   # nested string length
        ("metrics", [{} for _ in range(1000)]),
        ("timeline", [{} for _ in range(1000)]),
        ("hazard_detail", [{} for _ in range(1000)]),
        ("resources", [{} for _ in range(1000)]),
        ("bluf", [{"hazard": "a", "label": "b", "severity_class": "c"}] * 100),
        ("markdown", "x" * 300_000),
        ("summary", "x" * 5000),
        ("sources_ok", {str(i): True for i in range(200)}),          # dict cardinality
    ],
)
def test_broad_response_fields_are_bounded(field, value):
    with pytest.raises(ValidationError):
        BriefingResponse.model_validate({**_SAMPLE, field: value})


def test_pdf_endpoint_oversized_field_is_a_clean_422(client):
    """A field over its cap (body still < 2 MiB) is a bounded 422 — never a 500 or a render."""
    payload = {**_SAMPLE, "markdown": "x" * 300_000}  # ~300 KB body; markdown > 256 KiB cap
    resp = client.post("/v1/briefing/pdf", json=payload)
    assert resp.status_code == 422


# -- SA-08 #3: streaming body reject on the PDF path (no Content-Length) ----------------------

def test_pdf_body_stream_rejected_midway_without_content_length():
    """A chunked body over the cap is 413'd mid-stream; the downstream app is never reached."""
    reached = {"app": False}

    async def _downstream(scope, receive, send):
        reached["app"] = True

    mw = _MaxBodySizeMiddleware(_downstream, limits={"/v1/briefing/pdf": 100})
    scope = {"type": "http", "path": "/v1/briefing/pdf", "headers": []}  # no content-length
    chunks = iter(
        [
            {"type": "http.request", "body": b"a" * 60, "more_body": True},
            {"type": "http.request", "body": b"a" * 60, "more_body": False},  # 120 > 100 cap
        ]
    )

    async def receive():
        return next(chunks)

    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    asyncio.run(mw(scope, receive, send))

    assert reached["app"] is False  # rejected before the handler ran
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413


def test_pdf_path_is_registered_with_the_2mib_cap():
    """The PDF path is wired into the body-limit map at its own larger cap (regression guard)."""
    from upstreamwx.api.app import _PDF_MAX_BODY_BYTES, _body_limits

    assert _body_limits.get("/v1/briefing/pdf") == _PDF_MAX_BODY_BYTES
