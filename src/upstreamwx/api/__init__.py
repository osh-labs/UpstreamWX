"""UpstreamWX briefing API (M0.3) — engine + SITREP behind HTTP, cached and scheduled.

See :mod:`upstreamwx.api.app` for the FastAPI application and endpoints.
"""

from .app import app
from .service import BriefingService

__all__ = ["app", "BriefingService"]
