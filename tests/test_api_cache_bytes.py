"""Byte-budget and static-TTL cache tests (SA-02).

The in-process briefing cache was bounded by entry count only, so a few large entries could
retain gigabytes behind a modest "512 entry" cap. These tests pin the two new bounds:

* the LRU evicts oldest-first until the retained byte total fits ``maxbytes`` (not just the
  entry count), and
* deterministic static (inputs-replay) entries expire once older than ``static_ttl_s`` — they
  previously never expired.

All offline and deterministic: the TTL clock is injected via ``now`` (monotonic seconds).
"""

from __future__ import annotations

from upstreamwx.api.cache import STATIC_TOKEN, BoundedLRU, BriefingCache, _estimate_bytes


class _Stub:
    """A minimal stand-in for GeneratedBriefing carrying only the size-dominant field."""

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown


def _briefing(n_bytes: int) -> _Stub:
    """A stub whose _estimate_bytes is ``n_bytes`` + the fixed 2048 overhead."""
    return _Stub("x" * n_bytes)


# -- byte budget --------------------------------------------------------------------------
def test_briefing_cache_byte_budget_evicts_lru():
    # Each entry estimates to 10_000 + 2048 = 12_048 bytes; a 50_000 budget holds ~4.
    cache = BriefingCache(maxsize=1000, maxbytes=50_000)
    for i in range(10):
        cache.put(f"k{i}", _briefing(10_000), token="T")
    assert cache._store.total_bytes <= 50_000
    assert cache.get("k9", "T") is not None  # newest retained
    assert cache.get("k0", "T") is None      # oldest evicted


def test_bounded_lru_count_cap_without_bytes():
    """maxbytes=None keeps the pure count-cap behaviour (regression)."""
    lru: BoundedLRU[int] = BoundedLRU(maxsize=3)
    for i in range(5):
        lru.put(f"k{i}", i, size=1_000_000)  # size ignored when maxbytes is None
    assert len(lru) == 3
    assert lru.get("k4") == 4
    assert lru.get("k1") is None


def test_bounded_lru_overwrite_does_not_double_count():
    lru: BoundedLRU[int] = BoundedLRU(maxsize=10, maxbytes=1_000_000)
    lru.put("k", 1, size=10_000)
    first = lru.total_bytes
    lru.put("k", 2, size=10_000)  # same key
    assert lru.total_bytes == first == 10_000


def test_bounded_lru_keeps_sole_oversized_entry():
    """A single entry larger than the budget is retained, not evicted to empty."""
    lru: BoundedLRU[int] = BoundedLRU(maxsize=10, maxbytes=1_000)
    lru.put("only", 1, size=50_000)
    assert lru.get("only") == 1


# -- static TTL ---------------------------------------------------------------------------
def test_static_entry_ttl_expiry():
    cache = BriefingCache(static_ttl_s=5.0)
    cache.put("s", _briefing(100), token=STATIC_TOKEN, now=0.0)
    assert cache.get("s", "anytoken", now=3.0) is not None   # within TTL
    assert cache.get("s", "anytoken", now=10.0) is None       # expired -> evicted
    assert cache.get("s", "anytoken", now=10.5) is None       # stays gone after eviction


def test_live_entry_ignores_static_ttl():
    """A cycle-scoped (non-static) entry is unaffected by the static TTL."""
    cache = BriefingCache(static_ttl_s=5.0)
    cache.put("c", _briefing(100), token="2026-06-20T12Z", now=0.0)
    assert cache.get("c", "2026-06-20T12Z", now=10_000.0) is not None  # TTL does not apply
    assert cache.get("c", "2026-06-20T18Z", now=10_000.0) is None       # but the cycle check does


def test_static_entry_without_ttl_never_expires():
    """Default BriefingCache() keeps the original never-expiring static behaviour."""
    cache = BriefingCache()
    cache.put("s", _briefing(100), token=STATIC_TOKEN, now=0.0)
    assert cache.get("s", "anytoken", now=10_000_000.0) is not None


# -- estimator defensiveness --------------------------------------------------------------
def test_estimate_bytes_defensive_on_stub():
    """The estimator must not assume .result/.mission exist (test stubs lack them)."""
    assert _estimate_bytes(_Stub("hello")) == len(b"hello") + 2048
