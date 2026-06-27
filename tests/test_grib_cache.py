"""Offline tests for the shared in-process decoded-grid memo (roadmap §M0.1.1).

The disk cache (:func:`upstreamwx.grib.cache.cached_subset`) avoids re-downloading a cycle's
byte-range subset; :func:`upstreamwx.grib.cache.decode_cached` avoids re-*decoding* it with
cfgrib on every warm request. These exercise the memo semantics — hit reuse, and re-decode
when the underlying file changes (mtime/size) — with a trivial decode stand-in, no cfgrib.
"""

from __future__ import annotations

import os
from pathlib import Path

import upstreamwx.grib.cache as gc
from upstreamwx.grib.cache import decode_cached


def _counting_decode():
    calls: list[Path] = []

    def _decode(path: Path):
        calls.append(path)
        return f"decoded::{path.read_text()}"

    return _decode, calls


def test_decode_cached_memoises_repeat_hits(tmp_path: Path) -> None:
    """A second load of the same unchanged file reuses the decode (no re-decode)."""
    path = tmp_path / "subset.grib2"
    path.write_text("grid-A")
    decode, calls = _counting_decode()

    first = decode_cached(path, decode)
    second = decode_cached(path, decode)

    assert first == "decoded::grid-A"
    assert second is first  # same object handed back
    assert len(calls) == 1  # decoded once, served from memo on the second call


def test_decode_cached_redecodes_when_file_changes(tmp_path: Path) -> None:
    """Rewriting the file (new mtime/size — e.g. ``refresh``) misses the memo and re-decodes."""
    path = tmp_path / "subset.grib2"
    path.write_text("grid-A")
    decode, calls = _counting_decode()

    decode_cached(path, decode)
    # Rewrite with different content and bump mtime so the (path, mtime, size) key changes.
    path.write_text("grid-B-longer")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))

    again = decode_cached(path, decode)

    assert again == "decoded::grid-B-longer"
    assert len(calls) == 2  # the changed file was decoded afresh


def test_decode_cached_evicts_beyond_capacity(tmp_path: Path, monkeypatch) -> None:
    """The LRU is bounded: the oldest entry is evicted and re-decoded after overflow."""
    monkeypatch.setattr(gc, "_DECODE_CACHE_MAX", 2)
    gc._decoded.clear()
    decode, calls = _counting_decode()

    paths = []
    for i in range(3):
        p = tmp_path / f"s{i}.grib2"
        p.write_text(f"grid-{i}")
        paths.append(p)
        decode_cached(p, decode)  # 3 distinct files into a size-2 cache

    # s0 was evicted (oldest); re-loading it decodes again, while s2 is still memoised.
    decode_cached(paths[0], decode)
    after_s0 = len(calls)
    decode_cached(paths[2], decode)

    assert after_s0 == 4  # 3 initial decodes + 1 re-decode of the evicted s0
    assert len(calls) == after_s0  # s2 was still cached, so its hit added no decode
