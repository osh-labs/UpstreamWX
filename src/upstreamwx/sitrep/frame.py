"""Haiku natural-language framing (M0.2 stage 2; PRD FR-21).

Adds a plain-language BLUF narrative on top of the deterministic structured render
(:mod:`upstreamwx.sitrep.render`). The model is **strictly constrained to narrate**:
it may not add, remove, or alter any posture, tier, confidence, number, or window
(FR-20). To make that guarantee structural, framing only *prepends* a SUMMARY block —
every authoritative line produced by the renderer is left byte-for-byte untouched
below it, so the engine remains the sole source of every posture.
"""

from __future__ import annotations

import json

from ..config import get_settings
from ..engine.models import BriefingResult, Hazard

# FR-21: natural-language framing only, via Claude Haiku.
DEFAULT_MODEL = "claude-haiku-4-5"

_SUMMARY_HEADING = "## SUMMARY (plain language)"
_INSERT_BEFORE = "## BLUF"

_SYSTEM_PROMPT = (
    "You are a wilderness-weather briefing writer for a caving and canyoneering hazard "
    "tool. You will receive a structured hazard assessment as JSON. Write a short "
    "plain-language summary (2-4 sentences) that a trip leader can read at a glance.\n\n"
    "STRICT RULES:\n"
    "- Narrate ONLY what is in the JSON. Do not add, remove, soften, or escalate any "
    "hazard posture, tier, confidence level, number, time window, or driver.\n"
    "- Never give a go / no-go recommendation. This tool is reference-only.\n"
    "- Do not invent data, sources, or advice not present in the JSON.\n"
    "- Output the summary prose only — no headings, no lists, no preamble."
)


def _structured_view(result: BriefingResult) -> dict:
    """Compact JSON-serializable view of the engine result for the framer."""
    bluf = {}
    for hazard in Hazard:
        posture = result.bluf.get(hazard)
        if posture is None:
            continue
        window = posture.window_of_concern
        bluf[hazard.value] = {
            "posture": posture.severity_label,
            "confidence": posture.confidence.label if posture.confidence else None,
            "window_of_concern": (
                [window[0].isoformat(), window[1].isoformat()] if window else None
            ),
        }
    return {
        "activity_type": result.mission.activity_type.value,
        "overall_posture": result.overall_tier.label,
        "overall_confidence": result.overall_confidence.label,
        "phases_inferred": result.phases_inferred,
        "hazards": bluf,
    }


def _splice_summary(structured_md: str, narrative: str) -> str:
    """Insert the SUMMARY block above the BLUF; leave all structured lines untouched."""
    block = f"{_SUMMARY_HEADING}\n\n{narrative}\n\n"
    idx = structured_md.find(_INSERT_BEFORE)
    if idx == -1:  # defensive: no BLUF heading — append at top after the title block
        return structured_md
    return structured_md[:idx] + block + structured_md[idx:]


def frame_briefing(
    result: BriefingResult,
    structured_md: str,
    *,
    client=None,
    model: str = DEFAULT_MODEL,
) -> str:
    """Return ``structured_md`` with a Haiku-written plain-language summary prepended.

    The structured Markdown is treated as authoritative and is never modified — only a
    SUMMARY section is added above the BLUF (FR-20). If no client and no
    ``ANTHROPIC_API_KEY`` is available, framing is skipped and ``structured_md`` is
    returned unchanged (graceful degradation).
    """
    if client is None:
        api_key = get_settings().anthropic_api_key
        if not api_key:
            return structured_md
        import anthropic  # lazy: keep anthropic out of the import path when unused

        client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=model,
        max_tokens=400,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": json.dumps(_structured_view(result), sort_keys=True, indent=2),
            }
        ],
    )
    narrative = "".join(b.text for b in response.content if b.type == "text").strip()
    if not narrative:
        return structured_md
    return _splice_summary(structured_md, narrative)
