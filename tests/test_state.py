"""Unit tests for watch-history persistence."""

from __future__ import annotations

import json
import stat

from nstream import state
from nstream.config import Config

CFG = Config(torrentio_base="tb")


def test_make_entry_movie():
    e = state.make_entry("tt1", "Movie", "movie", 10.0, 100.0)
    assert e["video_id"] == "tt1"
    assert e["type"] == "movie"
    assert "series_id" not in e
    assert "season" not in e


def test_make_entry_series_from_video():
    e = state.make_entry(
        "tt1:1:2", "Show", "series", 5.0, 50.0,
        series_id="tt1", video={"season": 1, "episode": 2},
    )  # fmt: skip
    assert e["series_id"] == "tt1"
    assert e["season"] == 1
    assert e["episode"] == 2


def test_make_entry_series_explicit():
    e = state.make_entry(
        "tt1:3:4", "Show", "series", 5.0, 50.0, series_id="tt1", season=3, episode=4
    )
    assert (e["season"], e["episode"]) == (3, 4)


def test_watched_threshold():
    assert state._watched({"position": 95.0, "duration": 100.0}) is True
    assert state._watched({"position": 50.0, "duration": 100.0}) is False
    assert state._watched({"position": 10.0, "duration": 0.0}) is False


def test_watched_absolute_tail():
    # Within END_TAIL_SECONDS of the end counts as finished even below 0.9
    # (e.g. long credits / padded duration / mpv paused at EOF with keep-open).
    assert state._watched({"position": 8800.0, "duration": 8850.0}) is True  # 99.4%, <60s left
    assert state._watched({"position": 5000.0, "duration": 10000.0}) is False  # 50%, far from end


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_entry(CFG, state.make_entry("tt1", "A", "movie", 10.0, 100.0))
    hist = state.load_history(CFG)
    assert hist["tt1"]["title"] == "A"


def test_save_entry_chmod_600(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_entry(CFG, state.make_entry("tt1", "A", "movie", 10.0, 100.0))
    mode = stat.S_IMODE((tmp_path / "nstream" / "history.json").stat().st_mode)
    assert mode == 0o600


def test_watched_entry_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_entry(CFG, state.make_entry("tt1", "A", "movie", 10.0, 100.0))
    # Re-save past the watched threshold → entry removed.
    state.save_entry(CFG, state.make_entry("tt1", "A", "movie", 99.0, 100.0))
    assert "tt1" not in state.load_history(CFG)


def test_recent_sorted_and_filtered(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    e_old = state.make_entry("a", "Old", "movie", 10.0, 100.0)
    e_old["ts"] = 1.0
    e_new = state.make_entry("b", "New", "movie", 10.0, 100.0)
    e_new["ts"] = 2.0
    e_done = state.make_entry("c", "Done", "movie", 99.0, 100.0)  # watched → excluded
    e_done["ts"] = 3.0
    for e in (e_old, e_new, e_done):
        state.save_entry(CFG, e)
    recent = state.recent(CFG)
    assert [e["video_id"] for e in recent] == ["b", "a"]


def test_load_corrupt_history_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = tmp_path / "nstream"
    d.mkdir(parents=True)
    (d / "history.json").write_text("{ broken")
    assert state.load_history(CFG) == {}


def test_history_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = Config(torrentio_base="tb", history_enabled=False)
    state.save_entry(cfg, state.make_entry("tt1", "A", "movie", 10.0, 100.0))
    assert not (tmp_path / "nstream" / "history.json").exists()
    assert state.load_history(cfg) == {}


def test_save_entry_no_partial_tmp_left(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_entry(CFG, state.make_entry("tt1", "A", "movie", 10.0, 100.0))
    leftovers = list((tmp_path / "nstream").glob(".history-*.tmp"))
    assert leftovers == []
    # file is valid JSON
    json.loads((tmp_path / "nstream" / "history.json").read_text())
