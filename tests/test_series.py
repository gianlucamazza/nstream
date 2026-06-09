"""Unit tests for the series flow: binge loop, episode picker, history resume.

The player entry point is injected (`PlayVideo`), so these tests pass a fake that
records calls — no cli involved. The cli-side dispatch lives in test_cli.py."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nstream import series
from nstream.config import Config, HistoryEntry, Meta, PlayOpts, Video

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
        return (None, next_label is not None and idx < advance_until)

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
        return ("nessuno stream disponibile per «E1»", False)

    notice = series.binge(CFG, "tt", "Show", eps, eps[0], _opts(), play_video=play)
    assert notice == "nessuno stream disponibile per «E1»"


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
        return ("solo episodio 2", False)  # a notice → becomes the next header

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
        return ("non ancora disponibile", False)

    entry = HistoryEntry(video_id="tt:1", type="series", title="Show", series_id="tt")
    assert series.resume(CFG, entry, _opts(), play_video=play) == "non ancora disponibile"


# --- entry_video -------------------------------------------------------------


def test_entry_video_series_and_movie():
    e = HistoryEntry(type="series", season=2, episode=5)
    assert series.entry_video(e) == {"season": 2, "episode": 5}
    assert series.entry_video(HistoryEntry(type="movie")) is None
    assert series.entry_video(HistoryEntry()) is None  # legacy entries default to movie
