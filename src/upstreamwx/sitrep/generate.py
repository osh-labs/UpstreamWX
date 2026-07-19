"""Shared briefing generation: mission spec -> Markdown SITREP (M0.2 core, reused in M0.3).

Both the terminal command (:mod:`upstreamwx.sitrep.cli`) and the API
(:mod:`upstreamwx.api`) drive a briefing through the *same* path so their output is
identical in content by construction (roadmap §M0.3 exit criterion):

    mission -> ingest (or provided HazardInputs) -> engine.assess -> render_md -> frame

When ``inputs`` is supplied the live ingest is skipped and the briefing is rendered
from that saved feature vector (offline, reproducible — the corpus/golden path and the
deterministic-reproduction guarantee of FR-25). Otherwise the orchestrator gathers every
source with graceful degradation (NFR-6); source-availability is reported on the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import get_settings
from ..engine.assess import assess
from ..engine.models import BriefingResult, HazardInputs, Mission
from ..ingest.base import IngestBundle
from .frame import frame_briefing
from .render import render_md


@dataclass
class GeneratedBriefing:
    """A rendered briefing plus the provenance the CLI/API surface around it."""

    markdown: str
    result: BriefingResult
    generated_at: datetime
    framed: bool
    bundle: IngestBundle | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def sources_ok(self) -> dict[str, bool]:
        return dict(self.bundle.sources_ok) if self.bundle is not None else {}

    @property
    def degraded(self) -> bool:
        """True if any source was unavailable for this briefing (NFR-6)."""
        return any(ok is False for ok in self.sources_ok.values())


def generate_briefing(
    mission: Mission,
    *,
    inputs: HazardInputs | None = None,
    frame: bool | None = None,
    generated_at: datetime | None = None,
    cycle=None,
) -> GeneratedBriefing:
    """Generate a briefing for ``mission``.

    ``inputs`` — render from this saved feature vector instead of running live ingest.
    ``frame`` — add the Haiku narrative; ``None`` means "frame iff an API key is set"
    (FR-21). ``generated_at`` defaults to now (the only time-varying render input).
    ``cycle`` — optional SREF cycle override forwarded to the ingest orchestrator.
    """
    generated_at = generated_at or datetime.now(UTC)
    warnings: list[str] = []

    bundle: IngestBundle | None = None
    upstream = None
    phase_inputs = None
    if inputs is None:
        from ..ingest.base import to_phase_hazard_inputs
        from ..ingest.orchestrator import gather_inputs

        inputs, bundle = gather_inputs(mission, cycle=cycle)
        # The orchestrator delineates the upstream domain and attaches it to the
        # bundle; reuse it for the header so we don't trace twice (None on failure —
        # the header degrades rather than failing, NFR-6).
        upstream = bundle.upstream
        if upstream is None and bundle.sources_ok.get("watershed") is False:
            warnings.append("upstream delineation unavailable")
        # Phase-scoped feature vectors so the local hazards (heat/cold/lightning) respond to
        # the forecast *during* each phase; None when no forecast axis was gathered, and never
        # on the offline ``inputs`` path — assess then uses the single window vector unchanged.
        phase_inputs = to_phase_hazard_inputs(bundle, mission, inputs)

    result = assess(mission, inputs, phase_inputs=phase_inputs)
    structured = render_md(result, upstream=upstream, bundle=bundle, generated_at=generated_at)

    want_frame = frame if frame is not None else bool(get_settings().anthropic_api_key)
    markdown = frame_briefing(result, structured) if want_frame else structured

    return GeneratedBriefing(
        markdown=markdown,
        result=result,
        generated_at=generated_at,
        framed=want_frame,
        bundle=bundle,
        warnings=warnings,
    )
