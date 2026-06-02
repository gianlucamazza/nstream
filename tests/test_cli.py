"""Unit tests for cli pure helpers and the play() arg/signal contract."""

from __future__ import annotations

import argparse

import pytest

from nstream import cli
from nstream.config import Config, Video


def test_display_title_movie():
    assert cli.display_title("Dune", None) == "Dune"


def test_display_title_series_with_name():
    v = Video(season=1, episode=3, name="Ep")
    assert cli.display_title("Show", v) == "Show · S01E03 · Ep"


def test_display_title_series_without_name():
    assert cli.display_title("Show", Video(season=2, episode=10)) == "Show · S02E10"


@pytest.mark.parametrize(
    ("sec", "expected"),
    [(30, "0:30"), (95, "1:35"), (3725, "1:02:05"), (0, "0:00")],
)
def test_fmt_time(sec, expected):
    assert cli._fmt_time(sec) == expected


def _ns(**kw):
    base = {"subs": False, "sub_menu": False, "sub_lang": None}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.mark.parametrize(
    ("ns", "expected"),
    [
        (_ns(), (None, None)),
        (_ns(subs=True), ("auto", None)),
        (_ns(sub_menu=True), ("menu", None)),
        (_ns(sub_lang="eng"), ("auto", "eng")),
        (_ns(subs=True, sub_lang="ita"), ("auto", "ita")),  # sub_lang wins
    ],
)
def test_sub_options(ns, expected):
    assert cli._sub_options(ns) == expected


# --- pick_subtitles --------------------------------------------------------


CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])


@pytest.fixture
def stub_subs(monkeypatch):
    chosen = {}

    def fake_download(sub, work_dir):
        chosen["sub"] = sub
        return f"{work_dir}/{sub.get('lang')}.srt"

    monkeypatch.setattr(cli, "_download_subtitle", fake_download)
    return chosen


def test_pick_subtitles_auto_prefers_language(monkeypatch, stub_subs, tmp_path):
    subs = [{"lang": "fre", "url": "u"}, {"lang": "ita", "url": "u"}, {"lang": "eng", "url": "u"}]
    monkeypatch.setattr(cli.api, "subtitles", lambda *a, **k: list(subs))
    out = cli.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="auto")
    assert stub_subs["sub"]["lang"] == "ita"
    assert out and out[0].endswith("ita.srt")


def test_pick_subtitles_auto_no_preferred_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.api, "subtitles", lambda *a, **k: [{"lang": "fre", "url": "u"}])
    monkeypatch.setattr(cli, "_download_subtitle", lambda *a, **k: pytest.fail("must not download"))
    assert cli.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="auto") == ()


def test_pick_subtitles_lang_override(monkeypatch, stub_subs, tmp_path):
    subs = [{"lang": "ita", "url": "u"}, {"lang": "eng", "url": "u"}]
    monkeypatch.setattr(cli.api, "subtitles", lambda *a, **k: list(subs))
    cli.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="auto", lang="eng")
    assert stub_subs["sub"]["lang"] == "eng"


def test_pick_subtitles_menu_uses_fzf(monkeypatch, stub_subs, tmp_path):
    subs = [{"lang": "ita", "url": "u"}, {"lang": "eng", "url": "u"}]
    monkeypatch.setattr(cli.api, "subtitles", lambda *a, **k: list(subs))
    monkeypatch.setattr(cli, "fzf", lambda items, prompt: items[-1][1])  # pick last
    cli.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="menu")
    assert stub_subs["sub"]["lang"] == "eng"


def test_pick_subtitles_none_available(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.api, "subtitles", lambda *a, **k: [])
    assert cli.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="auto") == ()


# --- play() arg + signal contract -----------------------------------------


class _FakePopen:
    """Captures argv and simulates the Lua script writing the advance signal."""

    last_args: list[str] = []

    def __init__(self, args, *a, **k):
        type(self).last_args = args
        for x in args:
            if x.startswith("--script-opts-append=nstream-signal="):
                with open(x.split("=", 2)[2], "w") as f:
                    f.write("next\n")

    def wait(self):
        return 0

    def poll(self):
        return 0


@pytest.fixture
def stub_mpv(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(cli, "_track_position", lambda *a, **k: None)


def test_play_series_loads_script_and_advances(stub_mpv):
    cfg = Config(torrentio_base="tb", hwdec="", autoplay_lead=12)
    pos, dur, adv = cli.play(cfg, "Show · S01E02", "http://u", start=42, next_label="Show · S01E03")
    args = _FakePopen.last_args
    assert "--force-media-title=Show · S01E02" in args
    assert "--start=42" in args
    assert "--no-resume-playback" in args
    assert not any("write-filename" in a for a in args)
    assert any(a.startswith("--script=") and a.endswith("nstream.lua") for a in args)
    assert "--script-opts-append=nstream-lead=12" in args
    assert adv is True


def test_play_movie_no_script_no_advance(stub_mpv):
    cfg = Config(torrentio_base="tb", hwdec="")
    pos, dur, adv = cli.play(cfg, "Movie", "http://u", next_label=None)
    assert not any(a.startswith("--script=") for a in _FakePopen.last_args)
    assert adv is False


def test_play_hwdec_injected_when_configured(stub_mpv, monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_has_hwdec", lambda: False)
    cfg = Config(torrentio_base="tb", hwdec="auto-safe", mpv_args=[])
    cli.play(cfg, "Movie", "http://u")
    assert "--hwdec=auto-safe" in _FakePopen.last_args


def test_play_hwdec_not_injected_when_user_set(stub_mpv, monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_has_hwdec", lambda: True)
    cfg = Config(torrentio_base="tb", hwdec="auto-safe")
    cli.play(cfg, "Movie", "http://u")
    assert not any(a.startswith("--hwdec") for a in _FakePopen.last_args)


# --- binge loop (_play_series) ---------------------------------------------


def _episodes(n):
    return [Video(id=f"tt:{i}", season=1, episode=i, name=f"E{i}") for i in range(1, n + 1)]


def _stub_play_video(monkeypatch, advance_until):
    """Record each call; return advance=True while index < advance_until."""
    calls = []

    def fake(cfg, typ, video_id, title, opts, *, auto, next_label, on_save):
        calls.append({"video_id": video_id, "auto": auto, "next_label": next_label})
        idx = len(calls)  # 1-based
        # Mimic real play(): advancing requires the overlay, which requires a next_label.
        return (0, next_label is not None and idx < advance_until)

    monkeypatch.setattr(cli, "_play_video", fake)
    return calls


def test_binge_advances_then_stops(monkeypatch):
    eps = _episodes(4)
    calls = _stub_play_video(monkeypatch, advance_until=3)  # advance after ep1, ep2
    opts = cli.PlayOpts(auto=False, sub_mode=None, sub_lang=None, history=False, autoplay=True)
    rc = cli._play_series(CFG, "tt", "Show", eps, eps[0], opts)
    assert rc == 0
    assert [c["video_id"] for c in calls] == ["tt:1", "tt:2", "tt:3"]
    # first episode honours opts.auto (False); binge episodes force auto=True
    assert [c["auto"] for c in calls] == [False, True, True]


def test_binge_stops_at_last_episode(monkeypatch):
    eps = _episodes(2)
    calls = _stub_play_video(monkeypatch, advance_until=99)  # always wants to advance
    opts = cli.PlayOpts(auto=True, sub_mode=None, sub_lang=None, history=False, autoplay=True)
    cli._play_series(CFG, "tt", "Show", eps, eps[0], opts)
    assert len(calls) == 2  # no episode 3 to go to
    assert calls[-1]["next_label"] is None  # last episode offers no "next"


def test_binge_no_next_label_when_autoplay_off(monkeypatch):
    eps = _episodes(3)
    calls = _stub_play_video(monkeypatch, advance_until=99)
    opts = cli.PlayOpts(auto=True, sub_mode=None, sub_lang=None, history=False, autoplay=False)
    cli._play_series(CFG, "tt", "Show", eps, eps[0], opts)
    assert len(calls) == 1  # advance never offered → plays one and stops
    assert calls[0]["next_label"] is None


def test_binge_starts_from_chosen_episode(monkeypatch):
    eps = _episodes(4)
    calls = _stub_play_video(monkeypatch, advance_until=99)
    opts = cli.PlayOpts(auto=True, sub_mode=None, sub_lang=None, history=False, autoplay=True)
    cli._play_series(CFG, "tt", "Show", eps, eps[2], opts)  # start at E3
    assert [c["video_id"] for c in calls] == ["tt:3", "tt:4"]
