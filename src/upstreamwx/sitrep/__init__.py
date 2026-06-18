"""SITREP renderer/framer (M0.2).

Two-stage output layer over the engine's :class:`BriefingResult`:

1. :func:`render_md` — deterministic structured Markdown (golden-file testable).
2. :func:`frame_briefing` — optional Claude Haiku natural-language framing that narrates
   the structured object without changing any posture (FR-20/FR-21).
"""

from .frame import frame_briefing
from .render import DISCLAIMER, render_md

__all__ = ["render_md", "frame_briefing", "DISCLAIMER"]
