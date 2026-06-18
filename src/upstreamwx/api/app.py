"""UpstreamWX briefing API (M0.3).

Wraps the deterministic engine + M0.2 SITREP behind HTTP, with the server-side caching
and scheduled regeneration the PRD assumes (§7, §11; FR-12). The endpoint returns the
same briefing the CLI does for the same inputs (roadmap §M0.3) because both drive
:func:`upstreamwx.sitrep.generate.generate_briefing`.

Run locally::

    uvicorn upstreamwx.api.app:app --reload

Endpoints:
- ``POST /v1/briefing`` — mission spec -> briefing (structured + framed), cached.
- ``GET  /v1/health``   — liveness + the current refresh cycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..config import get_settings
from .cycles import cycle_key, next_cycle
from .models import BriefingResponse, MissionSpec
from .scheduler import run_scheduler
from .service import BriefingService

logger = logging.getLogger("upstreamwx.api")

# Single process-wide service so the cache and active-mission registry are shared across
# requests and the scheduler.
service = BriefingService()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the cycle-aligned refresh scheduler for the app's lifetime (FR-12)."""
    task: asyncio.Task | None = None
    stop = asyncio.Event()
    if get_settings().api_enable_scheduler:
        task = asyncio.create_task(run_scheduler(service, stop=stop))
        logger.info("briefing refresh scheduler started")
    try:
        yield
    finally:
        if task is not None:
            stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown best-effort
                pass


app = FastAPI(
    title="UpstreamWX Briefing API",
    version="0.3",
    summary="Mission-specific multi-hazard weather briefings (reference only).",
    lifespan=lifespan,
)


@app.get("/v1/health")
def health() -> dict:
    """Liveness probe plus the current/next refresh cycle and cache size."""
    return {
        "status": "ok",
        "cycle": cycle_key(),
        "next_cycle": next_cycle().isoformat(),
        "cached_briefings": len(service.cache),
        "active_missions": service.active_count,
    }


@app.post("/v1/briefing", response_model=BriefingResponse)
def briefing(spec: MissionSpec) -> BriefingResponse:
    """Generate (or return a cached) briefing for a mission spec.

    Non-mandatory source outages degrade gracefully (NFR-6): the briefing still renders
    with the missing input marked unavailable in ``sources_ok``/``degraded`` rather than
    erroring.
    """
    return service.get_briefing(spec)


def main() -> None:
    """Console entry point: serve the API with uvicorn (``upstreamwx-api``)."""
    import uvicorn

    uvicorn.run("upstreamwx.api.app:app", host="0.0.0.0", port=8000)  # noqa: S104
