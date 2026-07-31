"""Unit tests for watch-history persistence."""

from __future__ import annotations

import json
import stat

from nstream import state, util
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
    assert state.history._watched({"position": 95.0, "duration": 100.0}) is True
    assert state.history._watched({"position": 50.0, "duration": 100.0}) is False
    assert state.history._watched({"position": 10.0, "duration": 0.0}) is False


def test_watched_absolute_tail():
    # Within END_TAIL_SECONDS of the end counts as finished even below 0.9
    # (e.g. long credits / padded duration / mpv paused at EOF with keep-open).
    assert (
        state.history._watched({"position": 8800.0, "duration": 8850.0}) is True
    )  # 99.4%, <60s left
    assert (
        state.history._watched({"position": 5000.0, "duration": 10000.0}) is False
    )  # 50%, far from end


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


def test_watchlist_toggle_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    meta = {"id": "tt1", "type": "movie", "name": "A", "poster": "https://img"}
    assert state.toggle_watchlist(CFG, meta) is True
    assert state.is_watchlisted(CFG, "tt1") is True
    assert state.watchlist(CFG)[0]["name"] == "A"
    assert state.toggle_watchlist(CFG, meta) is False
    assert state.watchlist(CFG) == []


def test_recent_searches_are_deduplicated_and_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for i in range(state.MAX_RECENT_SEARCHES + 3):
        state.remember_search(CFG, f"Title {i}")
    state.remember_search(CFG, "title 5")
    queries = state.recent_searches(CFG)
    assert queries[0] == "title 5"
    assert len(queries) == state.MAX_RECENT_SEARCHES
    assert sum(q.casefold() == "title 5" for q in queries) == 1


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
    real_open = state.history.os.open

    def deny(path, *a, **k):
        if str(path).endswith(".history.lock"):
            raise OSError("no lock for you")
        return real_open(path, *a, **k)

    monkeypatch.setattr(state.history.os, "open", deny)
    state.save_entry(CFG, state.make_entry("tt1", "A", "movie", 10.0, 100.0))
    assert state.load_history(CFG)["tt1"]["position"] == 10.0  # unlocked but not blocked


# --- staleness guards + binge hygiene (round 2) ------------------------------


def _session(tmp_path, monkeypatch, entry, device="192.168.1.9"):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    state.remember_cast(CFG, entry, device)


def test_update_from_receiver_stale_ttl_clears(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, state.make_entry("tt1", "A", "movie", 0.0, 0.0))
    rs = util.RunState(state.CAST_SESSION)
    old = rs.read()
    assert old is not None
    old["ts"] = old["ts"] - state.CAST_SESSION_TTL - 3600
    rs.write(old)
    assert state.update_from_receiver(CFG, "192.168.1.9", 500.0, 3000.0) is False
    assert rs.read() is None  # dead session dropped, not left to corrupt later polls
    assert state.load_history(CFG) == {}


def test_update_from_receiver_title_mismatch_clears(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, state.make_entry("tt1", "Mr. Robot", "movie", 0.0, 0.0))
    ok = state.update_from_receiver(CFG, "192.168.1.9", 500.0, 3000.0, title="Big Buck Bunny")
    assert ok is False
    assert util.RunState(state.CAST_SESSION).read() is None
    assert state.load_history(CFG) == {}


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "receiver_title",
    [
        "Mr. Robot · S01E04 · eps1.3_da3m0ns.mp4",  # castbridge decorated display title
        "Mr.Robot.S01E04.1080p.WEB-DL.mkv",  # catt: release filename
    ],
)
def test_update_from_receiver_title_variants_match(tmp_path, monkeypatch, receiver_title):
    _session(tmp_path, monkeypatch, state.make_entry("tt1", "Mr. Robot", "movie", 0.0, 0.0))
    ok = state.update_from_receiver(CFG, "192.168.1.9", 500.0, 3000.0, title=receiver_title)
    assert ok is True
    assert state.load_history(CFG)["tt1"]["position"] == 500.0


def test_update_from_receiver_artifact_title_skips_guard(tmp_path, monkeypatch):
    # Tier-2 catt fallback: the receiver reports our own temp name → guard not applicable
    _session(tmp_path, monkeypatch, state.make_entry("tt1", "Mr. Robot", "movie", 0.0, 0.0))
    ok = state.update_from_receiver(CFG, "192.168.1.9", 500.0, 3000.0, title="cast-a1b2.mp4")
    assert ok is True


def test_note_started_retires_series_started_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    e1 = state.make_entry(
        "tt1:1:1", "Show", "series", 0.0, 0.0, series_id="tt1", season=1, episode=1
    )
    state.note_started(CFG, e1)
    e2 = state.make_entry(
        "tt1:1:2", "Show", "series", 0.0, 0.0, series_id="tt1", season=1, episode=2
    )
    state.note_started(CFG, e2)
    assert list(state.load_history(CFG)) == ["tt1:1:2"]  # binge leaves only the latest


def test_note_started_keeps_sibling_with_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_entry(
        CFG,
        state.make_entry(
            "tt1:1:1", "Show", "series", 600.0, 3000.0, series_id="tt1", season=1, episode=1
        ),
    )
    e2 = state.make_entry(
        "tt1:1:2", "Show", "series", 0.0, 0.0, series_id="tt1", season=1, episode=2
    )
    state.note_started(CFG, e2)
    assert set(state.load_history(CFG)) == {"tt1:1:1", "tt1:1:2"}  # real progress survives


def test_save_entry_prunes_aged_started_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    aged = state.make_entry("tt-old", "Old", "movie", 0.0, 0.0)
    aged["ts"] = aged["ts"] - state.STARTED_TTL - 86400
    state.save_entry(CFG, aged)
    keeper = state.make_entry("tt-real", "Real", "movie", 100.0, 3000.0)
    keeper["ts"] = keeper["ts"] - state.STARTED_TTL - 86400  # old but with real progress
    state.save_entry(CFG, keeper)
    state.save_entry(CFG, state.make_entry("tt-new", "New", "movie", 10.0, 100.0))
    assert set(state.load_history(CFG)) == {"tt-real", "tt-new"}  # aged started pruned


def test_clear_cast_session_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    state.clear_cast_session()  # nothing to clear: no error
    state.remember_cast(CFG, state.make_entry("tt1", "A", "movie", 0.0, 0.0), None)
    state.clear_cast_session()
    assert util.RunState(state.CAST_SESSION).read() is None


def test_expire_cast_session_by_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    state.remember_cast(CFG, state.make_entry("tt1", "A", "movie", 0.0, 0.0), None)
    state.expire_cast_session()  # fresh → kept
    rs = util.RunState(state.CAST_SESSION)
    assert rs.read() is not None
    stale = rs.read()
    assert stale is not None
    stale["ts"] = stale["ts"] - state.CAST_SESSION_TTL - 60
    rs.write(stale)
    state.expire_cast_session()
    assert rs.read() is None


def test_save_entry_keeps_watched_series_for_advance(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    e = state.make_entry(
        "tt1:1:4", "Show", "series", 2950.0, 3000.0, series_id="tt1", season=1, episode=4
    )
    state.save_entry(CFG, e)  # watched → kept for next-episode resume, not popped
    assert "tt1:1:4" in state.load_history(CFG)
    assert state.recent(CFG) == []  # …but hidden from continue-watching
    assert state.watched_series(CFG)[0]["episode"] == 4
    # a watched MOVIE is still retired
    state.save_entry(CFG, state.make_entry("tt9", "Film", "movie", 2950.0, 3000.0))
    assert "tt9" not in state.load_history(CFG)


def test_note_started_retires_watched_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.save_entry(
        CFG,
        state.make_entry(
            "tt1:1:4", "Show", "series", 2950.0, 3000.0, series_id="tt1", season=1, episode=4
        ),
    )
    nxt = state.make_entry(
        "tt1:1:5", "Show", "series", 0.0, 0.0, series_id="tt1", season=1, episode=5
    )
    state.note_started(CFG, nxt)  # the advance happened: the finished sibling retires
    assert list(state.load_history(CFG)) == ["tt1:1:5"]


# --- dead-source negative cache (ADR 0025) ---------------------------------


def test_mark_dead_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state.is_dead("abc") is False
    state.mark_dead("abc", "HTTP 404")
    assert state.is_dead("abc") is True
    assert state.dead_sources()["abc"]["reason"] == "HTTP 404"


def test_mark_dead_ignores_empty_key(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.mark_dead("", "nope")
    assert state.dead_sources() == {}


def test_dead_entries_expire(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.mark_dead("old", "gone")
    path = tmp_path / "nstream" / "dead-sources.json"
    data = json.loads(path.read_text())
    data["sources"]["old"]["ts"] -= state.DEAD_TTL + 1
    path.write_text(json.dumps(data))
    assert state.is_dead("old") is False  # a file may come back: the ban isn't eternal


def test_dead_cache_is_capped(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(state.dead, "MAX_DEAD_SOURCES", 3)
    for i in range(5):
        state.mark_dead(f"k{i}", "gone")
    entries = state.dead_sources()
    assert len(entries) <= 3
    assert "k4" in entries  # newest survives the prune


def test_forget_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    state.mark_dead("a", "gone")
    state.mark_dead("b", "gone")
    assert state.forget_dead() == 2
    assert state.dead_sources() == {}


def test_dead_sources_tolerates_corrupt_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = tmp_path / "nstream" / "dead-sources.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert state.dead_sources() == {}  # best-effort: state errors never block playback
