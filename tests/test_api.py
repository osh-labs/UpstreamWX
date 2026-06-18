"""API tests (M0.3) — offline, no network, no LLM.

Covers the roadmap §M0.3 exit criteria that are testable in an ephemeral container:
the endpoint returns the same briefing the CLI does for the same inputs, cache hit/miss
behaves correctly, and the validation corpus passes through the API path. (The always-on
scheduler cadence + cross-restart persistence are EC2-validated, M0.1.1.)
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from upstreamwx.api.app import app, service
from upstreamwx.api.models import MissionSpec
from upstreamwx.engine.assess import assess
from upstreamwx.engine.models import Hazard, HazardInputs
from upstreamwx.sitrep.cli import main as cli_main
from upstreamwx.sitrep.generate import generate_briefing

FIXTURES = Path(__file__).parent / "fixtures" / "sitrep"
CORPUS = Path(__file__).parent / "corpus"
SAMPLE_INPUTS = yaml.safe_load((FIXTURES / "sample_inputs.yaml").read_text())["inputs"]

FIXED_NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


def _spec(**overrides) -> MissionSpec:
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
    return MissionSpec(**base)


@pytest.fixture
def client():
    """A TestClient with the background scheduler disabled (no real loop in tests)."""
    os.environ["UPSTREAMWX_API_ENABLE_SCHEDULER"] = "0"
    service.cache.clear()
    with TestClient(app) as c:
        yield c
    service.cache.clear()


def _strip_generated_line(md: str) -> str:
    """Drop the single time-varying header line so content can be compared."""
    return "\n".join(line for line in md.splitlines() if not line.startswith("_Generated "))


def test_health(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["cycle"].endswith("Z")


def test_briefing_offline_inputs(client):
    resp = client.post("/v1/briefing", json=_spec().model_dump(mode="json"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["markdown"].startswith("# UPSTREAMWX — MISSION BRIEFING")
    assert "## BLUF" in body["markdown"]
    assert "## DISCLAIMER" in body["markdown"]
    assert body["overall_posture"] == "High"  # slot + 65% precip -> flash flood High
    assert body["framed"] is False
    assert body["cached"] is False
    # Explicit inputs are deterministic -> validity token is "static" (never expires).
    assert body["cache_cycle"] == "static"


def test_cache_hit_on_reopen(client):
    payload = _spec().model_dump(mode="json")
    first = client.post("/v1/briefing", json=payload).json()
    second = client.post("/v1/briefing", json=payload).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["markdown"] == second["markdown"]


def test_api_matches_cli(tmp_path, client):
    """Exit criterion: the API returns content identical to the CLI for the same inputs."""
    inputs_file = tmp_path / "inputs.yaml"
    inputs_file.write_text(yaml.safe_dump({"inputs": SAMPLE_INPUTS}))
    out = tmp_path / "cli.md"
    assert (
        cli_main(
            [
                "--lat", "37.0192", "--lon", "-111.9889",
                "--activity", "canyon",
                "--start", "2026-06-20T08:00", "--end", "2026-06-20T18:00",
                "--name", "Buckskin Gulch", "--slot",
                "--inputs", str(inputs_file), "--no-frame",
                "--out", str(out),
            ]
        )
        == 0
    )
    api_md = client.post("/v1/briefing", json=_spec().model_dump(mode="json")).json()["markdown"]
    assert _strip_generated_line(api_md) == _strip_generated_line(out.read_text())


def test_validation_corpus_through_api():
    """Exit criterion: the validation corpus passes through the API path.

    Each flash-flood boundary case is driven through the service as a canyon mission;
    the API's flash-flood posture must equal the deterministic engine's (the API never
    alters a posture — it renders ``assess`` output, FR-13).
    """
    service.cache.clear()
    cases = yaml.safe_load((CORPUS / "flash_flood.yaml").read_text())["cases"]
    for case in cases:
        inputs = HazardInputs(**case["inputs"])
        spec = _spec(slot=case.get("is_slot", False), inputs=case["inputs"], name=case["id"])
        mission = spec.to_mission()
        expected = assess(mission, inputs)
        resp = service.get_briefing(spec, now=FIXED_NOW)
        flood_label = expected.bluf[Hazard.FLASH_FLOOD].severity_label
        assert resp.overall_posture == expected.overall_tier.label, case["id"]
        assert f"| Flash flood | {flood_label} |" in resp.markdown, case["id"]
        # And the rendered content is exactly the engine path (identical to the CLI).
        direct = generate_briefing(
            mission, inputs=inputs, frame=False, generated_at=FIXED_NOW
        )
        assert resp.markdown == direct.markdown, case["id"]
    service.cache.clear()


def test_cave_isolation_posture(client):
    """A cave mission renders flash flood only in the technical span (FR-14c)."""
    resp = client.post(
        "/v1/briefing",
        json=_spec(activity="cave", slot=False, inputs={"sref_p_precip": 70}).model_dump(
            mode="json"
        ),
    )
    assert resp.status_code == 200
    assert "isolated from surface weather" in resp.json()["markdown"]
