"""Pin the HyRiver (pynhd / async-retriever) HTTP cache under ``data_dir`` (FR-2/FR-3, NFR-6).

The USGS WBD/NLDI watershed stack caches its HTTP responses in an aiohttp SQLite file whose
location ``async_retriever._utils.create_cachefile`` derives as::

    Path(os.getenv("HYRIVER_CACHE_NAME", Path("cache", "aiohttp_cache.sqlite")))
    ...
    fname.parent.mkdir(parents=True, exist_ok=True)

— i.e. **``./cache/`` relative to the process CWD** by default. Under the SA-06 atomic-release
layout the service's ``WorkingDirectory`` is the release tree, which is root-owned and
**read-only** to the runtime account, so that ``mkdir`` raises ``[Errno 30] Read-only file
system: 'cache'`` and *every* HUC resolve / raindrop trace fails — silently degrading the
upstream-watershed flash-flood domain (the product's technical centerpiece). This surfaced only
once the data dir was pinned correctly (before, the box failed earlier / ran a writable checkout).

Fix: point ``HYRIVER_CACHE_NAME`` at ``Settings.data_dir/hyriver`` (writable, already in the
unit's ``ReadWritePaths``) before any HyRiver call. Keyed to ``data_dir`` so the CLI, API, dev,
and tests all agree with the single source of truth; an explicit operator-set ``HYRIVER_CACHE_NAME``
is respected. Cheap and idempotent — safe to call at every watershed entry point.
"""

from __future__ import annotations

import logging
import os

from ..config import Settings, get_settings

logger = logging.getLogger("upstreamwx.watershed.hyriver")

_ENV = "HYRIVER_CACHE_NAME"
# The value we last set, so we can tell our own pin apart from an operator override.
_ours: str | None = None


def configure_hyriver_cache(settings: Settings | None = None) -> None:
    """Point HyRiver's on-disk HTTP cache at ``data_dir/hyriver`` unless overridden.

    Idempotent. Respects an ``HYRIVER_CACHE_NAME`` this module did not set (operator override).
    A non-writable ``data_dir`` is *not* masked here — the env var is still set so the failure
    reports the intended path, and the real permission error surfaces from the trace itself.
    """
    global _ours
    current = os.environ.get(_ENV)
    if current and current != _ours:
        return  # operator-provided value — leave it alone
    settings = settings or get_settings()
    path = settings.data_dir / "hyriver" / "aiohttp_cache.sqlite"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("HyRiver cache dir %s not creatable (%s) — trace may fail", path.parent, exc)
    os.environ[_ENV] = str(path)
    _ours = str(path)
