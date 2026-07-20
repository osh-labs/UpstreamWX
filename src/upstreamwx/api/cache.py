"""Server-side briefing cache, keyed by location/window (PRD §7, §11; FR-25).

Briefings are generated server-side and cached so reopening the app — or a scheduled
refresh hitting the same mission — costs nothing (roadmap §M0.3). A cache entry is
**valid for one SREF cycle** (:mod:`upstreamwx.api.cycles`): a request in a new cycle
misses and regenerates, which is exactly the cache-aligned refresh FR-12 wants. Requests
that pin an explicit ``HazardInputs`` are deterministic (no live data), so their entries
never expire — same inputs reproduce the same briefing record (FR-25, NFR-4).

This is an in-process store. Cross-restart persistence is deliberately deferred to
M0.1.1 (EC2): an ephemeral dev container cannot validate it, and the interface here
(``get``/``put`` keyed by a stable string) is what a persistent backend would implement.
It is **capacity-bounded** as an LRU (``maxsize``) so an always-on host cannot grow it
without limit — an evicted briefing simply regenerates on the next request (NFR-6).
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

from ..engine.models import HazardInputs, Mission
from ..sitrep.generate import GeneratedBriefing

# Validity token for deterministic (explicit-inputs) briefings: never expires.
STATIC_TOKEN = "static"

# Default LRU capacity when a caller does not pass one (kept in step with the
# ``api_cache_max_entries`` setting so a bare ``BriefingCache()`` is still bounded).
DEFAULT_MAX_ENTRIES = 512

_V = TypeVar("_V")


class BoundedLRU(Generic[_V]):
    """Thread-safe string-keyed LRU with a hard entry cap and an optional byte budget.

    A minimal building block for the in-process caches: ``get`` refreshes recency,
    ``put`` evicts the least-recently-used entries past ``maxsize``. Bounding these
    maps stops the unbounded per-mission growth that would otherwise leak on an
    always-on host (M0.1.1).

    A count cap alone does not bound *memory* — a few large entries can retain gigabytes
    behind a modest entry cap (SA-02). When ``maxbytes`` is set, callers pass each entry's
    estimated ``size`` to :meth:`put` and eviction continues (oldest first) until the total
    retained bytes fit the budget too, keeping at least the most-recent entry.
    """

    def __init__(self, maxsize: int = DEFAULT_MAX_ENTRIES, *, maxbytes: int | None = None) -> None:
        self._maxsize = max(1, maxsize)
        self._maxbytes = maxbytes
        self._store: OrderedDict[str, _V] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._total_bytes = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> _V | None:
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            return self._store[key]

    def put(self, key: str, value: _V, *, size: int = 0) -> None:
        with self._lock:
            if key in self._store:
                self._total_bytes -= self._sizes.get(key, 0)
            self._store[key] = value
            self._store.move_to_end(key)
            self._sizes[key] = size
            self._total_bytes += size
            # Evict oldest while over the entry cap, or over the byte budget with >1 entry
            # left (never evict the sole most-recent entry, even if it alone exceeds budget).
            while len(self._store) > self._maxsize or (
                self._maxbytes is not None
                and self._total_bytes > self._maxbytes
                and len(self._store) > 1
            ):
                old_key, _ = self._store.popitem(last=False)
                self._total_bytes -= self._sizes.pop(old_key, 0)

    def discard(self, key: str) -> None:
        """Remove ``key`` if present (used to evict a TTL-expired entry)."""
        with self._lock:
            if key in self._store:
                del self._store[key]
                self._total_bytes -= self._sizes.pop(key, 0)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._sizes.clear()
            self._total_bytes = 0


def mission_cache_key(
    mission: Mission, inputs: HazardInputs | None = None, *, units: str = "us"
) -> str:
    """Stable key identifying a mission's briefing (location + window + activity + metadata).

    Coordinates are rounded to ~11 m so a reopened pin at the same spot hits. When an
    explicit feature vector is supplied it is folded in, so two different saved inputs at
    one location/window do not collide. The Radius of Concern and Lightning Area of Concern
    radii are folded in too: both change the aggregation domain (and therefore the postures),
    so two requests differing only by a radius must not collide on one cache entry.

    The user-supplied mission metadata (``name``/``party_size``/``route_note``) is part of the
    key too (SA-04). The cached value embeds the request's ``Mission`` and the rendered briefing
    prints ``mission.name`` (render.py, structured.py), so omitting these fields let two
    differently-labelled missions at the same conditions collide — the second requester was
    served the first's mission name and presentation (a cross-user disclosure and a
    cache-poisoning vector). They are folded in as a single ``repr`` of the tuple so distinct
    metadata always yields a distinct key (a raw ``|``-join of attacker-chosen strings could
    otherwise be gamed into a collision). ``name`` is the only field rendered today;
    ``party_size``/``route_note`` are included defensively so a future change that surfaces them
    cannot reintroduce the leak.

    ``units`` (display system) is folded in too: it changes the rendered Markdown and structured
    values (°F vs °C, etc.), so two requests differing only by display units must not collide on
    one cached briefing. It does not change the engine result (FR-13, NFR-4).
    """
    parts = [
        mission.activity_type.value,
        f"{mission.lat:.4f}",
        f"{mission.lon:.4f}",
        mission.window_start.isoformat(),
        mission.window_end.isoformat(),
        mission.approach_end.isoformat() if mission.approach_end else "-",
        mission.egress_start.isoformat() if mission.egress_start else "-",
        "slot" if mission.is_slot else "open",
        f"roc={mission.radius_km}",
        f"laoc={mission.lightning_radius_km}",
        f"units={units}",
        f"meta={(mission.name, mission.party_size, mission.route_note)!r}",
    ]
    if inputs is not None:
        parts.append(repr(sorted(vars(inputs).items())))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


@dataclass
class _Entry:
    briefing: GeneratedBriefing
    token: str  # the cycle id (or STATIC_TOKEN) this entry is valid for
    stamp: float = 0.0  # monotonic insertion time — the static-entry TTL clock (SA-02)


def _estimate_bytes(briefing: object) -> int:
    """Estimate the retained size of a cached briefing (SA-02).

    A ``GeneratedBriefing`` is dominated by its rendered Markdown plus the retained
    ``Mission`` strings (``name``/``route_note``). Deliberately defensive — accessed via
    ``getattr`` so a lightweight test stub carrying only ``.markdown`` is handled — and an
    over-approximation (fixed overhead added) so the byte budget is a safe ceiling.
    """
    n = len(str(getattr(briefing, "markdown", "")).encode("utf-8"))
    result = getattr(briefing, "result", None)
    mission = getattr(result, "mission", None)
    n += len(getattr(mission, "name", "") or "") + len(getattr(mission, "route_note", "") or "")
    return n + 2048  # fixed overhead for the structured/result object graph


class BriefingCache:
    """Thread-safe in-process briefing cache with cycle-scoped validity, LRU-bounded.

    Bounded on two axes (SA-02): the entry count (``maxsize``) and, when ``maxbytes`` is set,
    the estimated retained bytes. Deterministic static (inputs-replay) entries never expire by
    cycle, so an optional ``static_ttl_s`` bounds their lifetime instead — a pinned replay entry
    cannot persist for the whole process lifetime.
    """

    def __init__(
        self,
        maxsize: int = DEFAULT_MAX_ENTRIES,
        *,
        maxbytes: int | None = None,
        static_ttl_s: float | None = None,
    ) -> None:
        self._store: BoundedLRU[_Entry] = BoundedLRU(maxsize, maxbytes=maxbytes)
        self._static_ttl_s = static_ttl_s

    def get(self, key: str, token: str, *, now: float | None = None) -> GeneratedBriefing | None:
        """Return the cached briefing iff present and valid for ``token`` (else None).

        A static entry stays valid across cycles but expires once older than ``static_ttl_s``
        (evicted on read); a live entry is valid only for the cycle ``token`` it was stored for.
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.token == STATIC_TOKEN:
            if self._static_ttl_s is not None:
                now = time.monotonic() if now is None else now
                if now - entry.stamp > self._static_ttl_s:
                    self._store.discard(key)  # expired replay entry — evict and miss
                    return None
            return entry.briefing
        if entry.token == token:
            return entry.briefing
        return None

    def put(
        self, key: str, briefing: GeneratedBriefing, token: str, *, now: float | None = None
    ) -> None:
        now = time.monotonic() if now is None else now
        self._store.put(
            key, _Entry(briefing=briefing, token=token, stamp=now), size=_estimate_bytes(briefing)
        )

    def __len__(self) -> int:
        return len(self._store)

    def clear(self) -> None:
        self._store.clear()
