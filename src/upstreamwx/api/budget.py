"""Per-principal and global fair-use / cost budgets for the access gate (SA-01).

Once a request carries an app-issued principal (:mod:`upstreamwx.api.auth`), expensive and
billable work is charged against rolling windows so no single client — and no aggregate of
clients — can run the host or the model bill away:

- **per-principal** windows enforce fairness (e.g. N cold briefings/hour, N framing calls/day);
- **global** windows are absolute ceilings / circuit breakers (notably the daily model-spend
  cap), independent of any one principal.

The per-IP token buckets already in ``api/app.py`` (SA-02) sit *beneath* these as the
IP-aggregate layer, so minting many tokens from one source is still bounded — the three
layers together answer the audit's "IP-only throttling is readily shared, rotated, or
bypassed". Counters are in-process (single-worker deployment; the shared-store version is the
same M0.1.1 upgrade the briefing cache documents) and thread-safe. Limits are read by the
caller from settings and passed in per charge, so a deployment can tune them without restart.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict

# LRU cap over tracked keys so the counters can never become a memory sink (mirrors the
# rate limiter's _RATE_LIMIT_MAX_IPS). An evicted idle key simply starts a fresh window.
_MAX_KEYS = 8192


class BudgetExceeded(Exception):
    """A per-principal budget was hit (maps to HTTP 429 + Retry-After)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("per-principal budget exceeded")
        self.retry_after = retry_after


class GlobalCeiling(Exception):
    """A global ceiling / circuit breaker was hit (maps to HTTP 503 + Retry-After)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("global budget ceiling reached")
        self.retry_after = retry_after


class WindowCounter:
    """Thread-safe fixed-window per-key counter, LRU-bounded.

    ``charge`` admits up to ``limit`` events per ``window_s`` for a key and returns ``None``
    when admitted, else the whole seconds until the window resets (a Retry-After). A
    ``limit`` <= 0 disables the counter (always admits) so a budget can be turned off.
    """

    def __init__(self, *, max_keys: int = _MAX_KEYS) -> None:
        self._max_keys = max(1, max_keys)
        # key -> (window_start_monotonic, count_in_window)
        self._windows: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = threading.Lock()

    def charge(
        self, key: str, *, limit: int, window_s: float, now: float | None = None
    ) -> int | None:
        if limit <= 0:
            return None
        now = time.monotonic() if now is None else now
        with self._lock:
            start, count = self._windows.pop(key, (now, 0))
            if now - start >= window_s:  # window elapsed → reset
                start, count = now, 0
            if count < limit:
                self._windows[key] = (start, count + 1)  # (re)insert as most-recently-used
                while len(self._windows) > self._max_keys:
                    self._windows.popitem(last=False)
                return None
            self._windows[key] = (start, count)
            return max(1, math.ceil(window_s - (now - start)))

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


class BudgetEnforcer:
    """Per-principal + global charge points, one instance shared across requests (SA-01)."""

    def __init__(self) -> None:
        self._principal = WindowCounter()
        # Few global keys (one per kind); a small cap keeps them from ever being evicted.
        self._global = WindowCounter(max_keys=64)

    def charge_principal(self, kind: str, pid: str, *, limit: int, window_s: float) -> None:
        retry_after = self._principal.charge(f"{kind}:{pid}", limit=limit, window_s=window_s)
        if retry_after is not None:
            raise BudgetExceeded(retry_after)

    def charge_global(self, kind: str, *, limit: int, window_s: float) -> None:
        retry_after = self._global.charge(kind, limit=limit, window_s=window_s)
        if retry_after is not None:
            raise GlobalCeiling(retry_after)

    def reset(self) -> None:
        """Clear all counters (per app run, so TestClient contexts stay isolated)."""
        self._principal.reset()
        self._global.reset()
