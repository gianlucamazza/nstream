"""Unit tests for the series flow: binge loop, episode picker, history resume.

The player entry point is injected (`PlayVideo`), so these tests pass a fake that
records calls — no cli involved. The cli-side dispatch lives in test_cli.py."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nstream import series
from nstream.config import Config, PlayOpts
from nstream.types import HistoryEntry, Meta, Video

CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])


def _opts(*, auto: bool = True, autoplay: bool = True) -> PlayOpts:
    return PlayOpts(
        auto=auto, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=autoplay
    )


def _episodes(n):
    return [Video(id=f"tt:{i}", season=1, episode=i, name=f"E{i}") for i in range(1, n + 1)]


def _fake_play(advance_until):
    """A PlayVideo stub: record each call; return advance=True while index < advance_until."""
    calls = []

    def fake(
        video_id,
        title,
        opts,
        *,
        auto,
        next_label,
        on_save,
        reselect_on_wrong_audio=True,
        cast_meta=None,
    ):
        calls.append(
            {
                "video_id": video_id,
                "title": title,
                "auto": auto,
                "next_label": next_label,
                "reselect": reselect_on_wrong_audio,
                "cast_meta": cast_meta,
            }
        )
        idx = len(calls)  # 1-based
        # Mimic real play(): advancing requires the overlay, which requires a next_label.
        # Third return is quality (0 = Auto) for binge sticky.
        return (None, next_label is not None and idx < advance_until, 0)

    return fake, calls


# --- binge loop (auto-advance) ----------------------------------------------


def test_binge_advances_then_stops():
    eps = _episodes(4)
    play, calls = _fake_play(advance_until=3)  # advance after ep1, ep2
    opts = _opts(auto=False)
    assert series.binge(CFG, "tt", "Show", eps, eps[0], opts, play_video=play) is None
    assert [c["video_id"] for c in calls] == ["tt:1", "tt:2", "tt:3"]
    # first episode honours opts.auto (False); binge episodes force auto=True
    assert [c["auto"] for c in calls] == [False, True, True]
    # the first episode may block on a reselection; unattended advances must not
    assert [c["reselect"] for c in calls] == [True, False, False]


def test_binge_stops_at_last_episode():
    eps = _episodes(2)
    play, calls = _fake_play(advance_until=99)  # always wants to advance
    series.binge(CFG, "tt", "Show", eps, eps[0], _opts(), play_video=play)
    assert len(calls) == 2  # no episode 3 to go to
    assert calls[-1]["next_label"] is None  # last episode offers no "next"


def test_binge_no_next_label_when_autoplay_off():
    eps = _episodes(3)
    play, calls = _fake_play(advance_until=99)
    series.binge(CFG, "tt", "Show", eps, eps[0], _opts(autoplay=False), play_video=play)
    assert len(calls) == 1  # advance never offered → plays one and stops
    assert calls[0]["next_label"] is None


def test_binge_starts_from_chosen_episode():
    eps = _episodes(4)
    play, calls = _fake_play(advance_until=99)
    series.binge(CFG, "tt", "Show", eps, eps[2], _opts(), play_video=play)  # start at E3
    assert [c["video_id"] for c in calls] == ["tt:3", "tt:4"]


def test_binge_stops_and_propagates_notice():
    """An episode with no streams stops the binge and surfaces its notice."""
    eps = _episodes(3)

    def play(*a, **k):
        return ("nessuno stream disponibile per «E1»", False, 0)

    notice = series.binge(CFG, "tt", "Show", eps, eps[0], _opts(), play_video=play)
    assert notice == "nessuno stream disponibile per «E1»"


def test_binge_sticky_quality():
    """Quality chosen on ep1 is threaded into opts for subsequent episodes (no re-pick)."""
    eps = _episodes(3)
    seen_quality: list[int | None] = []

    def play(video_id, title, opts, *, auto, next_label, on_save, **kw):
        seen_quality.append(opts.quality)
        # First episode "picks" 1080p; later ones should see quality=1080 on opts.
        return (None, next_label is not None and video_id != "tt:2", 1080)

    assert series.binge(CFG, "tt", "Show", eps, eps[0], _opts(), play_video=play) is None
    assert seen_quality == [None, 1080]  # ep1 undecided; ep2 sticky 1080


def test_binge_unknown_start_is_noop():
    eps = _episodes(2)
    play, calls = _fake_play(advance_until=99)
    other = Video(id="zz:9", season=9, episode=9)
    assert series.binge(CFG, "tt", "Show", eps, other, _opts(), play_video=play) is None
    assert calls == []


def test_binge_builds_cast_meta_per_episode():
    eps = _episodes(2)
    play, calls = _fake_play(advance_until=99)
    series.binge(CFG, "tt", "Show", eps, eps[0], _opts(), play_video=play, poster="P")
    metas = [c["cast_meta"] for c in calls]
    assert [(m.poster, m.series_title, m.season, m.episode) for m in metas] == [
        ("P", "Show", 1, 1),
        ("P", "Show", 1, 2),
    ]


# --- episode picker (series.play) -------------------------------------------


def _fzf_script(monkeypatch, returns):
    """A stub fzf_key yielding `returns` in order, recording headers (cf. test_cli)."""
    seen = SimpleNamespace(headers=[], preview=None)
    pending = iter(returns)

    def fake(items, prompt, *, header=None, expect=("tab",), preview=None):
        seen.headers.append(header)
        seen.preview = preview
        return next(pending)

    monkeypatch.setattr(series, "fzf_key", fake)
    return seen


META = Meta(id="tt", type="series", name="Show", poster="P")


def _picker_kwargs(play):
    return {
        "play_video": play,
        "pick_hint": lambda opts: "HINT",
        "apply_key": lambda opts, key: opts,
    }


def test_play_without_episodes_returns_notice(monkeypatch):
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: [])
    play, _ = _fake_play(advance_until=0)
    notice = series.play(CFG, META, _opts(), **_picker_kwargs(play))
    assert notice == "nessun episodio per «Show»"


def test_play_esc_leaves_picker(monkeypatch):
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: _episodes(2))
    seen = _fzf_script(monkeypatch, [None])
    play, calls = _fake_play(advance_until=0)
    assert series.play(CFG, META, _opts(), **_picker_kwargs(play)) is None
    assert calls == [] and seen.headers == ["HINT"]


def test_play_picks_episode_then_loops_threading_header(monkeypatch):
    eps = _episodes(3)
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: eps)
    seen = _fzf_script(monkeypatch, [("", eps[1]), None])

    def play(video_id, title, opts, **kw):
        return ("solo episodio 2", False, 0)  # a notice → becomes the next header

    assert series.play(CFG, META, _opts(), **_picker_kwargs(play)) is None
    assert seen.headers == ["HINT", "solo episodio 2"]
    # the preview token is the episode form preview.py expects
    assert seen.preview(eps[1]) == "episode tt 1 2"


def test_play_applies_key_to_opts(monkeypatch):
    """Tab/Alt-C reach the binge via apply_key (auto flip recorded by the fake)."""
    eps = _episodes(1)
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: eps)
    _fzf_script(monkeypatch, [("tab", eps[0]), None])
    play, calls = _fake_play(advance_until=0)
    kwargs = _picker_kwargs(play)
    kwargs["apply_key"] = lambda opts, key: opts if key != "tab" else _opts(auto=False)
    series.play(CFG, META, _opts(auto=True), **kwargs)
    assert calls[0]["auto"] is False  # the flipped opts drove the first episode


def test_play_season_first_for_multi_season(monkeypatch):
    """≥2 seasons → season menu first; ESC on episodes returns to seasons; ESC on seasons leaves."""
    eps = [
        Video(id="tt:1:1", season=1, episode=1, name="A"),
        Video(id="tt:2:1", season=2, episode=1, name="B"),
    ]
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: eps)
    season_picks = iter([1, None])  # pick S01, then ESC seasons
    monkeypatch.setattr(series, "fzf", lambda items, prompt, **k: next(season_picks))
    ep_picks = iter([None])  # ESC episodes → back to seasons
    monkeypatch.setattr(series, "fzf_key", lambda items, prompt, **k: next(ep_picks, None))
    play, calls = _fake_play(advance_until=0)
    assert series.play(CFG, META, _opts(), **_picker_kwargs(play)) is None
    assert calls == []


def test_play_flat_when_single_season_short(monkeypatch):
    """One short season stays a flat episode list (no season menu)."""
    eps = _episodes(3)  # all S01
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: eps)
    season_called = []
    monkeypatch.setattr(
        series,
        "fzf",
        lambda *a, **k: (
            season_called.append(1) or (_ for _ in ()).throw(AssertionError("no season menu"))
        ),
    )
    _fzf_script(monkeypatch, [None])
    play, _ = _fake_play(advance_until=0)
    assert series.play(CFG, META, _opts(), **_picker_kwargs(play)) is None
    assert season_called == []


# --- history resume (series.resume) -----------------------------------------


def test_resume_binges_rest_of_season(monkeypatch):
    eps = _episodes(3)
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: eps)
    play, calls = _fake_play(advance_until=99)
    entry = HistoryEntry(video_id="tt:2", type="series", title="Show", series_id="tt")
    assert series.resume(CFG, entry, _opts(), play_video=play) is None
    assert [c["video_id"] for c in calls] == ["tt:2", "tt:3"]


def test_resume_falls_back_to_single_episode(monkeypatch):
    """Entry not in the episode list (or no autoplay) → replay just that video."""
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: _episodes(2))
    play, calls = _fake_play(advance_until=99)
    entry = HistoryEntry(
        video_id="tt:9", type="series", title="Show", series_id="tt", season=1, episode=9
    )
    assert series.resume(CFG, entry, _opts(), play_video=play) is None
    assert [c["video_id"] for c in calls] == ["tt:9"]
    assert calls[0]["next_label"] is None  # no binge → no next-episode overlay
    assert calls[0]["title"] == "Show · S01E09"


def test_resume_no_autoplay_plays_single(monkeypatch):
    monkeypatch.setattr(
        series.api, "episodes", lambda cfg, sid: pytest.fail("episodes fetched without autoplay")
    )
    play, calls = _fake_play(advance_until=99)
    entry = HistoryEntry(
        video_id="tt:1", type="series", title="Show", series_id="tt", season=1, episode=1
    )
    assert series.resume(CFG, entry, _opts(autoplay=False), play_video=play) is None
    assert [c["video_id"] for c in calls] == ["tt:1"]


def test_resume_propagates_notice(monkeypatch):
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: [])

    def play(video_id, title, opts, **kw):
        return ("non ancora disponibile", False, 0)

    entry = HistoryEntry(video_id="tt:1", type="series", title="Show", series_id="tt")
    assert series.resume(CFG, entry, _opts(), play_video=play) == "non ancora disponibile"


# --- entry_video -------------------------------------------------------------


def test_entry_video_series_and_movie():
    e = HistoryEntry(type="series", season=2, episode=5)
    assert series.entry_video(e) == {"season": 2, "episode": 5}
    assert series.entry_video(HistoryEntry(type="movie")) is None
    assert series.entry_video(HistoryEntry()) is None  # legacy entries default to movie


# --- the continuation policy (ADR 0029) -------------------------------------


def _multi_season(per_season=(2, 2)):
    """Episodes across seasons, in the canonical (season, episode) order api.episodes uses."""
    return [
        Video(id=f"tt:{s}:{e}", season=s, episode=e, name=f"S{s}E{e}")
        for s, n in enumerate(per_season, start=1)
        for e in range(1, n + 1)
    ]


def _entry(season, episode, *, position=10.0, duration=100.0, vid=None):
    return HistoryEntry(
        video_id=vid or f"tt:{season}:{episode}", type="series", title="Show", series_id="tt",
        season=season, episode=episode, position=position, duration=duration,
    )  # fmt: skip


def test_next_video_crosses_the_season_boundary():
    eps = _multi_season((2, 2))
    assert series.next_video(eps, {"season": 1, "episode": 2})["id"] == "tt:2:1"


def test_next_video_none_past_the_finale():
    assert series.next_video(_multi_season((2, 2)), {"season": 2, "episode": 2}) is None


def test_next_video_unknown_position_never_restarts():
    """A legacy entry with no season/episode is not "before S01E01": advancing from it
    would silently restart the series."""
    assert series.next_video(_multi_season(), {"season": 0, "episode": 0}) is None
    assert series.next_video(_multi_season(), None) is None


def test_next_video_skips_gaps():
    eps = [Video(id="a", season=1, episode=1), Video(id="c", season=1, episode=3)]
    assert series.next_video(eps, {"season": 1, "episode": 1})["id"] == "c"


def test_next_up_finished_season_finale_advances_to_next_season(monkeypatch):
    eps = _multi_season((2, 2))
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: eps)
    nu = series.next_up(CFG, _entry(1, 2, position=99.0))  # watched finale of S01
    assert nu.selection == "next" and nu.video["id"] == "tt:2:1"


def test_next_up_series_finale_is_completed(monkeypatch):
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: _multi_season((2, 2)))
    nu = series.next_up(CFG, _entry(2, 2, position=99.0))
    assert nu.selection == "completed" and nu.video is None


def test_next_up_unfinished_episode_resumes_without_touching_the_catalogue(monkeypatch):
    monkeypatch.setattr(
        series.api, "episodes", lambda cfg, sid: pytest.fail("no episode fetch on resume")
    )
    nu = series.next_up(CFG, _entry(1, 2, position=10.0))
    assert nu.selection == "resume" and nu.video["episode"] == 2


def test_next_up_movie_resumes(monkeypatch):
    entry = HistoryEntry(video_id="tt1", type="movie", title="Film", position=99.0, duration=100.0)
    assert series.next_up(CFG, entry) == series.NextUp(None, "resume")


def test_next_up_empty_catalogue_resumes(monkeypatch):
    """An unreadable episode list is a catalogue hiccup, not a reason to block playback."""
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: [])
    nu = series.next_up(CFG, _entry(1, 2, position=99.0))
    assert nu.selection == "resume" and nu.video["episode"] == 2


def test_next_up_watched_entry_without_position_resumes(monkeypatch):
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: _multi_season())
    entry = _entry(0, 0, position=99.0, vid="tt:legacy")
    assert series.next_up(CFG, entry).selection == "resume"


def test_binge_crosses_the_season_boundary(monkeypatch):
    """The gap the suite never covered: every binge test used season=1 only."""
    eps = _multi_season((2, 2))
    play, calls = _fake_play(advance_until=99)
    notice = series.binge(CFG, "tt", "Show", eps, eps[1], _opts(), play_video=play)
    assert notice is None
    assert [c["video_id"] for c in calls] == ["tt:1:2", "tt:2:1", "tt:2:2"]
    assert calls[0]["next_label"].startswith("Show · S02E01")  # overlay names the next season


def test_resume_watched_episode_starts_from_the_next_one(monkeypatch):
    """Parity with the headless `-c`: a finished episode continues, it doesn't replay."""
    eps = _multi_season((2, 2))
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: eps)
    play, calls = _fake_play(advance_until=0)
    series.resume(CFG, _entry(1, 2, position=99.0), _opts(), play_video=play)
    assert [c["video_id"] for c in calls] == ["tt:2:1"]


def test_resume_finished_series_says_so(monkeypatch):
    monkeypatch.setattr(series.api, "episodes", lambda cfg, sid: _multi_season((2, 2)))
    play, calls = _fake_play(advance_until=0)
    notice = series.resume(CFG, _entry(2, 2, position=99.0), _opts(), play_video=play)
    assert notice is not None and "finita" in notice
    assert calls == []  # nothing replayed
