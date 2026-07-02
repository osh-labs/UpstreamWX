"""REFS subset cache self-heal — symmetric with the GEFS path.

A REFS byte-range subset fetched mid-publish can be truncated; eccodes then fails to decode
it (classically ``EOFError``). Left in place it degrades REFS on every briefing for the life of
the cached cycle. :func:`load_probability_field_cached` discards the bad artifact and re-fetches
once (data quality first-class, NFR-6). Hermetic — no network or GRIB I/O.
"""

from __future__ import annotations

import pytest

from upstreamwx.config import Settings
from upstreamwx.refs import cache as rcache
from upstreamwx.refs.sources import RefsCycle


def _stub_subset(monkeypatch, tmp_path):
    """Make cached_subset return a fake on-disk path without any download."""
    path = tmp_path / "refs.f009.APCP.gt12p7.grib2"
    path.write_bytes(b"stub")
    calls: list[bool] = []

    def fake_cached_subset(p, *, refresh=False, **kw):
        calls.append(refresh)
        path.write_bytes(b"stub")  # a "re-fetch" re-materializes the file
        return path, None

    monkeypatch.setattr(rcache, "cached_subset", fake_cached_subset)
    return path, calls


def test_corrupt_refs_subset_self_heals(monkeypatch, tmp_path):
    path, refresh_calls = _stub_subset(monkeypatch, tmp_path)
    discarded: list = []
    monkeypatch.setattr(rcache, "_discard_subset", lambda p: discarded.append(p))

    seen = {"n": 0}

    def flaky_decode(p, fn, **kw):
        seen["n"] += 1
        if seen["n"] == 1:
            raise EOFError("end of file reached while decoding GRIB message")

        class _DA:  # minimal stand-in for the decoded DataArray
            sizes = {"step": 1}

        return _DA()

    monkeypatch.setattr(rcache, "decode_cached", flaky_decode)

    field = rcache.load_probability_field_cached(
        RefsCycle("20260620", 0), 9, "APCP", ">12.7", settings=Settings(data_dir=tmp_path),
    )

    assert seen["n"] == 2                 # decoded again after the heal
    assert discarded == [path]            # the bad artifact was dropped
    assert refresh_calls[-1] is True      # re-fetched with refresh=True
    assert field.grib_path == path


def test_persistently_corrupt_refs_subset_raises(monkeypatch, tmp_path):
    _stub_subset(monkeypatch, tmp_path)
    monkeypatch.setattr(rcache, "_discard_subset", lambda p: None)

    def always_eof(p, fn, **kw):
        raise EOFError("still truncated")

    monkeypatch.setattr(rcache, "decode_cached", always_eof)

    with pytest.raises(EOFError):
        rcache.load_probability_field_cached(
            RefsCycle("20260620", 0), 9, "APCP", ">12.7", settings=Settings(data_dir=tmp_path),
        )
