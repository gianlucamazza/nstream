"""Unit tests for source availability (`availability`)."""

from __future__ import annotations

import pytest

from nstream import availability, state, tracks
from nstream.config import Config
from nstream.types import Stream


@pytest.fixture(autouse=True)
def _clear_probe_memo():
    availability.clear_memo()
    tracks.clear_cache()
    yield
    availability.clear_memo()
    tracks.clear_cache()


def _probe(monkeypatch, duration, calls=None):
    """Stub `tracks.probe_tracks` with a fixed duration, optionally counting calls."""

    def fake(url, **kw):
        if calls is not None:
            calls.append(url)
        return tracks.Tracks(duration=duration)

    monkeypatch.setattr(availability.tracks, "probe_tracks", fake)


def test_source_key_prefers_infohash():
    assert availability.source_key({"infoHash": "ABC123"}) == "abc123"
    assert availability.source_key({"behaviorHints": {"filename": "M.mkv"}}) == "file:M.mkv"
    assert (
        availability.source_key({"name": "[RD+] Torrentio\n1080p"}) == "name:[RD+] Torrentio 1080p"
    )
    assert availability.source_key({}) == ""


def test_expected_bytes_from_announced_size():
    assert availability.expected_bytes({"name": "x\n💾 7.16 GB"}) == int(7.16 * 1024**3)
    assert availability.expected_bytes({"name": "x\n1080p"}) == 0


def test_prune_dead_filters_known_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.mark_dead("abc", "gone")
    dead = {"infoHash": "abc", "url": "http://d"}
    alive = {"infoHash": "xyz", "url": "http://a"}
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    kept, dropped = availability.prune_dead(cfg, [dead, alive])
    assert dropped == 1 and kept == [alive]


def test_prune_dead_noop_on_local_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.mark_dead("abc", "gone")
    dead = {"infoHash": "abc"}
    cfg = Config(torrentio_base="tb", playback_backend="local")
    assert availability.prune_dead(cfg, [dead]) == ([dead], 0)


# --- duration vetting (ADR 0028) --------------------------------------------


def test_vet_duration_rejects_placeholder(monkeypatch):
    # The incident: a 30s "removed for copyright" clip in place of a ~55 min episode.
    _probe(monkeypatch, 30.0)
    v = availability.vet_duration("http://s", 3300.0)
    assert not v.ok
    assert v.duration == 30.0 and v.expected == 3300.0
    assert "0:30" in v.reason and "55" in v.reason  # both numbers, so the JSON reads honestly


def test_vet_duration_accepts_plausible(monkeypatch):
    _probe(monkeypatch, 3180.0)
    assert availability.vet_duration("http://s", 3300.0).ok


def test_vet_duration_accepts_longer_than_expected(monkeypatch):
    # One-way guard: extended cuts / double episodes / packs are LONGER, never rejected.
    _probe(monkeypatch, 7200.0)
    assert availability.vet_duration("http://s", 3300.0).ok


def test_vet_duration_unknown_expected_passes_without_probing(monkeypatch):
    monkeypatch.setattr(
        availability.tracks, "probe_tracks", lambda *a, **k: pytest.fail("must not probe")
    )
    assert availability.vet_duration("http://s", 0.0).ok


def test_vet_duration_unreadable_passes(monkeypatch):
    _probe(monkeypatch, 0.0)  # ffprobe missing/failed/timed out → benefit of the doubt
    assert availability.vet_duration("http://s", 3300.0).ok


def test_vet_duration_ignores_shorts(monkeypatch):
    _probe(monkeypatch, 30.0)
    assert availability.vet_duration("http://s", 300.0).ok  # below MIN_EXPECTED_S


def test_vet_duration_boundary_ratio(monkeypatch):
    expected = 3300.0
    floor = expected * availability.MIN_RUNTIME_RATIO
    _probe(monkeypatch, floor)
    assert availability.vet_duration("http://s", expected).ok  # exactly at the ratio passes
    tracks.clear_cache()
    _probe(monkeypatch, floor - 1)
    assert not availability.vet_duration("http://s", expected).ok


def test_vet_duration_uses_memoized_probe(monkeypatch):
    # Goes through the real `tracks.probe_tracks` so the per-url memo is exercised: the
    # guard must cost zero ffprobe on any path that already probed the same url.
    calls = []

    class _Proc:
        stdout = '{"format": {"duration": "3180.0"}, "streams": []}'

    monkeypatch.setattr(tracks.util, "run_cmd", lambda cmd, **kw: calls.append(cmd) or _Proc())
    assert availability.vet_duration("http://s", 3300.0).ok
    assert availability.vet_duration("http://s", 3300.0).ok
    assert len(calls) == 1


def test_drop_streams_removes_by_identity():
    a: Stream = {"name": "same"}
    b: Stream = {"name": "same"}  # equal by value, a different row
    results: list[Stream] = [a, b]
    availability.drop_streams(results, [a])
    assert len(results) == 1 and results[0] is b
