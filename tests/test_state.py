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


def test_recent_typed_filters_and_legacy_defaults_to_movie(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    e_movie = state.make_entry("m1", "Film", "movie", 10.0, 100.0)
    e_series = state.make_entry(
        "s1", "Serie", "series", 10.0, 100.0, series_id="s", season=1, episode=2
    )
    e_legacy = state.make_entry("l1", "Legacy", "movie", 10.0, 100.0)
    del e_legacy["type"]  # pre-series entry without "type"
    for e in (e_movie, e_series, e_legacy):
        state.save_entry(CFG, e)
    movies = {e["video_id"] for e in state.recent(CFG, typ="movie")}
    series = {e["video_id"] for e in state.recent(CFG, typ="series")}
    untyped = {e["video_id"] for e in state.recent(CFG)}
    assert movies == {"m1", "l1"}  # legacy entry counts as movie
    assert series == {"s1"}
    assert untyped == {"m1", "s1", "l1"}  # default stays mixed


# --- resume / near-end (keep-open) -----------------------------------------


def test_resume_position_skips_finished(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # finished entry (near end) → no resume
    state.save_entry(CFG, {"video_id": "v1", "position": 100.0, "duration": 100.0, "ts": 1.0})
    assert state.resume_position(CFG, "v1") is None


def test_resume_position_returns_and_clamps(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_entry(CFG, {"video_id": "v2", "position": 500.0, "duration": 10000.0, "ts": 1.0})
    assert state.resume_position(CFG, "v2") == 500.0  # 5%, far from end → resume


def test_resume_position_none_without_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state.resume_position(CFG, "missing") is None


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


# --- fire-and-return resume: note_started / cast session --------------------


def test_note_started_preserves_known_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_entry(CFG, state.make_entry("tt1", "A", "movie", 40.0, 100.0))
    state.note_started(CFG, state.make_entry("tt1", "A", "movie", 40.0, 0.0))
    e = state.load_history(CFG)["tt1"]
    assert (e["position"], e["duration"]) == (40.0, 100.0)  # duration inherited


def test_note_started_new_title_keeps_zero_duration(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.note_started(CFG, state.make_entry("tt2", "B", "movie", 0.0, 0.0))
    e = state.load_history(CFG)["tt2"]
    assert (e["position"], e["duration"]) == (0.0, 0.0)  # visible to -c, never "watched"


def test_remember_cast_update_from_receiver_merges(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    entry = state.make_entry(
        "tt1:1:1", "Show", "series", 0.0, 0.0, series_id="tt1", season=1, episode=1
    )
    state.remember_cast(CFG, entry, "192.168.1.9")
    assert state.update_from_receiver(CFG, "192.168.1.9", 500.0, 3000.0) is True
    e = state.load_history(CFG)["tt1:1:1"]
    assert (e["position"], e["duration"], e["season"]) == (500.0, 3000.0, 1)
    assert "device" not in e  # session-only field never lands in history


def test_update_from_receiver_device_mismatch_no_write(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    state.remember_cast(CFG, state.make_entry("tt1", "A", "movie", 0.0, 0.0), "192.168.1.9")
    assert state.update_from_receiver(CFG, "10.0.0.1", 500.0, 3000.0) is False
    assert state.load_history(CFG) == {}


def test_update_from_receiver_clear_is_one_shot(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    state.remember_cast(CFG, state.make_entry("tt1", "A", "movie", 0.0, 0.0), "192.168.1.9")
    assert state.update_from_receiver(CFG, "192.168.1.9", 500.0, 3000.0, clear=True) is True
    # session cleared → a later update has nothing to attribute the position to
    assert state.update_from_receiver(CFG, "192.168.1.9", 600.0, 3000.0) is False


def test_update_from_receiver_idle_zero_clears_but_keeps_history(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    state.remember_cast(CFG, state.make_entry("tt1", "A", "movie", 0.0, 0.0), "192.168.1.9")
    assert state.update_from_receiver(CFG, "192.168.1.9", 0.0, 0.0, clear=True) is False
    assert state.update_from_receiver(CFG, "192.168.1.9", 1.0, 2.0) is False  # cleared anyway


def test_update_from_receiver_watched_retires_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    state.save_entry(CFG, state.make_entry("tt1", "A", "movie", 10.0, 3000.0))
    state.remember_cast(CFG, state.make_entry("tt1", "A", "movie", 10.0, 3000.0), None)
    # stopped in the credits → save_entry's watched logic retires the entry
    assert state.update_from_receiver(CFG, None, 2990.0, 3000.0, clear=True) is True
    assert state.load_history(CFG) == {}


def test_history_lock_degrades_when_unopenable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    real_open = state.os.open

    def deny(path, *a, **k):
        if str(path).endswith(".history.lock"):
            raise OSError("no lock for you")
        return real_open(path, *a, **k)

    monkeypatch.setattr(state.os, "open", deny)
    state.save_entry(CFG, state.make_entry("tt1", "A", "movie", 10.0, 100.0))
    assert state.load_history(CFG)["tt1"]["position"] == 10.0  # unlocked but not blocked
