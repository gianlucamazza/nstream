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
    # No mpv.conf hwdec; concrete nstream value → injected as-is.
    monkeypatch.setattr(cli, "_mpv_conf_get", lambda opt: None)
    cfg = Config(torrentio_base="tb", hwdec="vaapi", mpv_args=[])
    cli.play(cfg, "Movie", "http://u")
    assert "--hwdec=vaapi" in _FakePopen.last_args


def test_play_hwdec_not_injected_when_user_set(stub_mpv, monkeypatch):
    # A concrete method in mpv.conf is respected, nothing injected.
    monkeypatch.setattr(cli, "_mpv_conf_get", lambda opt: "vaapi" if opt == "hwdec" else None)
    cfg = Config(torrentio_base="tb", hwdec="auto-safe")
    cli.play(cfg, "Movie", "http://u")
    assert not any(a.startswith("--hwdec") for a in _FakePopen.last_args)


# --- hwdec auto→vaapi upgrade (_hwdec_defaults) -----------------------------


def test_hwdec_auto_upgraded_to_detected(monkeypatch):
    """mpv.conf auto-safe + VAAPI detected → nstream pins --hwdec=vaapi (CLI wins)."""
    monkeypatch.setattr(cli, "_mpv_conf_get", lambda opt: "auto-safe" if opt == "hwdec" else None)
    monkeypatch.setattr(cli.quality, "detect_caps", lambda *a, **k: cli.quality.Caps(vaapi=True))
    monkeypatch.setattr(cli.quality, "preferred_hwdec", lambda caps: "vaapi")
    assert cli._hwdec_defaults(Config(torrentio_base="tb", hwdec="auto-safe")) == ["--hwdec=vaapi"]


def test_hwdec_auto_no_detection_defers_to_conf(monkeypatch):
    """auto in mpv.conf but no GPU detected → leave mpv.conf in charge."""
    monkeypatch.setattr(cli, "_mpv_conf_get", lambda opt: "auto-safe" if opt == "hwdec" else None)
    monkeypatch.setattr(cli.quality, "detect_caps", lambda *a, **k: cli.quality.Caps(vaapi=False))
    monkeypatch.setattr(cli.quality, "preferred_hwdec", lambda caps: None)
    assert cli._hwdec_defaults(Config(torrentio_base="tb", hwdec="auto-safe")) == []


def test_hwdec_mpv_args_override_defers(monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_get", lambda opt: "auto-safe")
    cfg = Config(torrentio_base="tb", hwdec="auto-safe", mpv_args=["--hwdec=foo"])
    assert cli._hwdec_defaults(cfg) == []


def test_hwdec_disabled_when_empty(monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_get", lambda opt: None)
    assert cli._hwdec_defaults(Config(torrentio_base="tb", hwdec="")) == []


# --- language preference (--alang/--slang) ---------------------------------


def test_lang_defaults_injected(monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"], subtitle_langs=["ita", "eng"])
    flags = cli._lang_defaults(cfg)
    assert "--alang=ita,eng" in flags
    assert "--slang=ita,eng" in flags
    assert "--subs-with-matching-audio=no" in flags


def test_lang_defaults_not_when_user_set_in_mpv_args(monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_args=["--alang=fre"])
    flags = cli._lang_defaults(cfg)
    assert not any(f.startswith("--alang") for f in flags)
    assert any(f.startswith("--slang") for f in flags)  # slang still injected


def test_lang_defaults_not_when_in_mpv_conf(monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_has", lambda opt: opt == "slang")
    cfg = Config(torrentio_base="tb")
    flags = cli._lang_defaults(cfg)
    assert any(f.startswith("--alang") for f in flags)
    assert not any(f.startswith("--slang") for f in flags)


def test_future_release_parsing():
    assert cli._future_release("2999-12-18T00:00:00.000Z") is not None  # far future
    assert cli._future_release("2000-01-01T00:00:00.000Z") is None  # past
    assert cli._future_release(None) is None
    assert cli._future_release("not-a-date") is None


def test_no_streams_message_upcoming(monkeypatch):
    monkeypatch.setattr(cli.api, "meta", lambda *a, **k: {"released": "2999-12-18T00:00:00.000Z"})
    msg = cli._no_streams_message(Config(torrentio_base="tb"), "movie", "tt1", "Dune 3")
    assert "non ancora disponibile" in msg and "18/12/2999" in msg


def test_no_streams_message_released(monkeypatch):
    monkeypatch.setattr(cli.api, "meta", lambda *a, **k: {"released": "2000-01-01T00:00:00.000Z"})
    msg = cli._no_streams_message(Config(torrentio_base="tb"), "movie", "tt1", "Old Film")
    assert "nessuno stream disponibile" in msg


def test_meta_label_upcoming_future_year():
    label = cli.meta_label({"type": "movie", "name": "X", "releaseInfo": "2999"})
    assert "in uscita" in label


def test_meta_label_no_hint_past_year():
    label = cli.meta_label({"type": "movie", "name": "X", "releaseInfo": "2000"})
    assert "in uscita" not in label


def test_quiet_defaults_injected(monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_quiet=True)
    flags = cli._quiet_defaults(cfg)
    assert flags and flags[0].startswith("--msg-level=")


def test_quiet_defaults_off(monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_quiet=False)
    assert cli._quiet_defaults(cfg) == []


def test_quiet_defaults_not_when_user_sets_msg_level(monkeypatch):
    monkeypatch.setattr(cli, "_mpv_conf_has", lambda opt: opt == "msg-level")
    cfg = Config(torrentio_base="tb", mpv_quiet=True)
    assert cli._quiet_defaults(cfg) == []


# --- resume / near-end (keep-open) -----------------------------------------


def test_resume_position_skips_finished(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = Config(torrentio_base="tb")
    # finished entry (near end) → no resume
    cli.state.save_entry(cfg, {"video_id": "v1", "position": 100.0, "duration": 100.0, "ts": 1.0})
    assert cli._resume_position(cfg, "v1") is None


def test_resume_position_returns_and_clamps(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = Config(torrentio_base="tb")
    cli.state.save_entry(cfg, {"video_id": "v2", "position": 500.0, "duration": 10000.0, "ts": 1.0})
    assert cli._resume_position(cfg, "v2") == 500.0  # 5%, far from end → resume


def test_resume_position_none_without_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert cli._resume_position(Config(torrentio_base="tb"), "missing") is None


def test_play_video_no_save_when_duration_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(cli, "play", lambda *a, **k: (42.0, 0.0, False))  # duration unobserved
    saved = []
    opts = cli.PlayOpts(auto=True, sub_mode=None, sub_lang=None, history=True, autoplay=False)
    cli._play_video(
        cfg, "movie", "tt1", "M", opts, auto=True, next_label=None,
        on_save=lambda p, d: saved.append((p, d)),
    )  # fmt: skip
    assert saved == []  # nothing persisted without a real duration


def test_play_video_no_crash_on_empty_stream_name(monkeypatch):
    """Regression: a stream with an empty name must not raise IndexError."""
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": ""}])
    monkeypatch.setattr(cli, "play", lambda *a, **k: (10.0, 100.0, False))
    opts = cli.PlayOpts(auto=True, sub_mode=None, sub_lang=None, history=False, autoplay=False)
    notice, advance = cli._play_video(
        cfg, "movie", "tt1", "Movie", opts, auto=True, next_label=None, on_save=None
    )
    assert (notice, advance) == (None, False)


def test_play_video_no_streams_returns_notice(monkeypatch):
    """No streams → return a user-facing notice (surfaced as the menu header)."""
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [])
    monkeypatch.setattr(cli.api, "meta", lambda *a, **k: {})  # released unknown → generic
    opts = cli.PlayOpts(auto=True, sub_mode=None, sub_lang=None, history=False, autoplay=False)
    notice, advance = cli._play_video(
        cfg, "movie", "tt1", "Dune 3", opts, auto=True, next_label=None, on_save=None
    )
    assert advance is False
    assert notice and "Dune 3" in notice


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
        return (None, next_label is not None and idx < advance_until)

    monkeypatch.setattr(cli, "_play_video", fake)
    return calls


def test_binge_advances_then_stops(monkeypatch):
    eps = _episodes(4)
    calls = _stub_play_video(monkeypatch, advance_until=3)  # advance after ep1, ep2
    opts = cli.PlayOpts(auto=False, sub_mode=None, sub_lang=None, history=False, autoplay=True)
    assert cli._play_series(CFG, "tt", "Show", eps, eps[0], opts) is None
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


def test_binge_stops_and_propagates_notice(monkeypatch):
    """An episode with no streams stops the binge and surfaces its notice."""
    eps = _episodes(3)

    def fake(*a, **k):
        return ("nessuno stream disponibile per «E1»", False)

    monkeypatch.setattr(cli, "_play_video", fake)
    opts = cli.PlayOpts(auto=True, sub_mode=None, sub_lang=None, history=False, autoplay=True)
    notice = cli._play_series(CFG, "tt", "Show", eps, eps[0], opts)
    assert notice == "nessuno stream disponibile per «E1»"


# --- navigation: back-to-list + home menu ----------------------------------


def _fzf_script(returns):
    """A stub fzf that yields `returns` in order and records the headers it saw."""
    seen = {"headers": [], "i": 0}

    def fake(items, prompt, *, header=None):
        seen["headers"].append(header)
        val = returns[seen["i"]]
        seen["i"] += 1
        return val

    return fake, seen


def test_pick_meta_loops_until_esc_and_threads_header(monkeypatch):
    """_pick_meta replays the list after a pick (back-to-list) and shows the
    playback notice as the next header; ESC (None) leaves with rc 0."""
    items = [("Dune", {"id": "tt1", "type": "movie", "name": "Dune"})]
    fake_fzf, seen = _fzf_script([items[0][1], None])  # pick once, then ESC
    monkeypatch.setattr(cli, "fzf", fake_fzf)
    monkeypatch.setattr(cli, "play_meta", lambda *a, **k: "non ancora disponibile")
    opts = cli.PlayOpts(auto=False, sub_mode=None, sub_lang=None, history=False, autoplay=False)
    assert cli._pick_meta(items, CFG, opts) == 0
    # First render has no header; after the pick the notice is threaded through.
    assert seen["headers"] == [None, "non ancora disponibile"]


def test_run_home_dispatches_actions(monkeypatch):
    """Home menu routes search/browse/settings then exits on ESC."""
    actions = [(cli._SEARCH, ""), (cli._BROWSE, "popolari"), (cli._SETTINGS, ""), None]
    fake_fzf, _ = _fzf_script(actions)
    monkeypatch.setattr(cli, "fzf", fake_fzf)
    monkeypatch.setattr(cli, "input", lambda *a: "matrix", raising=False)
    called = {"search": 0, "browse": [], "settings": 0}
    monkeypatch.setattr(cli, "run_search", lambda c, q, o: called.__setitem__("search", q))
    monkeypatch.setattr(cli, "run_browse", lambda c, cat, o: called["browse"].append(cat))
    monkeypatch.setattr(cli.settings, "run_settings", lambda c: called.__setitem__("settings", 1))
    monkeypatch.setattr(cli, "load", lambda: CFG)
    opts = cli.PlayOpts(auto=False, sub_mode=None, sub_lang=None, history=False, autoplay=False)
    assert cli.run_home(CFG, opts) == 0
    assert called["search"] == "matrix"
    assert called["browse"] == [cli.CAT_MAP["popolari"]]
    assert called["settings"] == 1


def test_fzf_passes_header_to_argv(monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "0\tlabel\n"

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli.fzf([("a", 1), ("b", 2)], "p> ", header="avviso")
    assert "--header" in captured["cmd"]
    assert "avviso" in captured["cmd"]
