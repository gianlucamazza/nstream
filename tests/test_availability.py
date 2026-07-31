"""Unit tests for source availability (`availability`)."""

from __future__ import annotations

import pytest

from nstream import availability, state
from nstream.config import Config


@pytest.fixture(autouse=True)
def _clear_probe_memo():
    availability.clear_memo()
    yield
    availability.clear_memo()


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
