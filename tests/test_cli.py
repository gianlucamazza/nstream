"""Unit tests for cli orchestration: pickers, the play-flow wiring, and navigation.

Low-level playback/cast/picker units live in test_player.py, test_caster.py and
test_picker.py; here we exercise cli's coordination, mocking play/cast/_resolve_device
(re-exported from those modules) as cli globals."""

from __future__ import annotations

import argparse

import pytest

from nstream import cli
from nstream.config import Config, HistoryEntry, Meta, Stream, Video


def test_main_preview_dispatch(monkeypatch):
    # `nstream __preview …` is handled before argparse and forwarded to preview.run_preview.
    seen = {}
    monkeypatch.setattr(cli.sys, "argv", ["nstream", "__preview", "title", "movie", "tt1"])
    monkeypatch.setattr(cli.preview, "run_preview", lambda argv: (seen.update(argv=argv), 0)[1])
    assert cli.main() == 0
    assert seen["argv"] == ["title", "movie", "tt1"]


def _gopts(*, auto=True, cast=False):
    return cli.PlayOpts(
        auto=auto, cast=cast, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )


def test_audio_langs_of_trusts_preferred_tag(monkeypatch):
    def boom(url):
        raise AssertionError("ffprobe should be skipped for a preferred-tagged release")

    monkeypatch.setattr(cli.tracks, "probe_tracks", boom)
    s: Stream = {"url": "u", "title": "Movie.2024.1080p.ITA.ENG.x264-GRP"}
    assert cli._audio_langs_of(Config(torrentio_base="tb", audio_langs=["ita"]), s) == {"ita"}


def test_audio_langs_of_probes_untagged(monkeypatch):
    monkeypatch.setattr(
        cli.tracks, "probe_tracks", lambda url: cli.tracks.Tracks(audio=[cli.tracks.Track(1, "es")])
    )
    s: Stream = {"url": "u", "title": "Some.Movie.2024.1080p.x264-GRP"}  # untagged
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    assert cli._audio_langs_of(cfg, s) == {"spa"}  # "es" → spa via the registry


def test_audio_langs_of_does_not_trust_multi(monkeypatch):
    # "Dual"/"MULTI" is ambiguous (may be Latino+Eng, no Italian); it must be probed,
    # not trusted as carrying a preferred track.
    calls = []
    monkeypatch.setattr(
        cli.tracks,
        "probe_tracks",
        lambda url: (
            calls.append(url)
            or cli.tracks.Tracks(audio=[cli.tracks.Track(1, "spa"), cli.tracks.Track(2, "eng")])
        ),
    )
    s: Stream = {"url": "u", "title": "Dune.Part.Two.2024.Dual.1080p.x265-YG"}
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    assert cli._audio_langs_of(cfg, s) == {"spa", "eng"}  # real tracks, not the "multi" guess
    assert calls  # the probe actually ran


def test_audio_langs_of_reads_title_when_untagged(monkeypatch):
    # language=und but the track title names the language → recovered via track_lang.
    monkeypatch.setattr(
        cli.tracks,
        "probe_tracks",
        lambda url: cli.tracks.Tracks(audio=[cli.tracks.Track(1, "und", title="Italian [TrueHD]")]),
    )
    s: Stream = {"url": "u", "title": "Movie.2024.1080p.x264"}
    assert cli._audio_langs_of(Config(torrentio_base="tb", audio_langs=["ita"]), s) == {"ita"}


def test_audio_langs_of_unverifiable_returns_none(monkeypatch):
    monkeypatch.setattr(cli.tracks, "probe_tracks", lambda url: cli.tracks.Tracks())
    s: Stream = {"url": "u", "title": "Some.Movie.2024.1080p.x264-GRP"}
    assert cli._audio_langs_of(Config(torrentio_base="tb", audio_langs=["ita"]), s) is None


def test_audio_langs_of_no_preference_returns_none():
    s: Stream = {"url": "u", "title": "x"}
    assert cli._audio_langs_of(Config(torrentio_base="tb", audio_langs=[]), s) is None


def test_play_video_guard_reselects_on_wrong_audio(monkeypatch, capsys):
    foreign: Stream = {"url": "u1", "name": "x\n1080p"}
    chosen2: Stream = {"url": "u2", "name": "y\n1080p"}
    picks = iter([foreign, chosen2])
    monkeypatch.setattr(cli.api, "streams", lambda *a: [foreign, chosen2])
    monkeypatch.setattr(cli, "_pick_stream", lambda *a, **k: next(picks))
    monkeypatch.setattr(cli, "_audio_langs_of", lambda cfg, ch: {"spa"})  # no ita/eng
    monkeypatch.setattr(cli, "_reselect_for_primary", lambda *a, **k: None)  # no better source
    got = {}
    monkeypatch.setattr(
        cli, "_play_on_mpv", lambda cfg, chosen, work_dir, **k: got.update(c=chosen) or (1.0, 2.0, False)
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"], history_enabled=False)
    cli._play_video(cfg, "movie", "tt1", "T", _gopts(), auto=True, next_label=None, on_save=None)
    assert got["c"] is chosen2  # reselected after the warning
    assert "nessuna traccia audio ita,eng" in capsys.readouterr().err


def test_play_video_guard_binge_warns_and_proceeds(monkeypatch, capsys):
    foreign: Stream = {"url": "u1", "name": "x\n1080p"}
    calls = []
    monkeypatch.setattr(cli.api, "streams", lambda *a: [foreign])
    monkeypatch.setattr(cli, "_pick_stream", lambda *a, **k: (calls.append(1), foreign)[1])
    monkeypatch.setattr(cli, "_audio_langs_of", lambda cfg, ch: {"spa"})
    monkeypatch.setattr(cli, "_reselect_for_primary", lambda *a, **k: None)
    got = {}
    monkeypatch.setattr(
        cli, "_play_on_mpv", lambda cfg, chosen, work_dir, **k: got.update(c=chosen) or (1.0, 2.0, False)
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"], history_enabled=False)
    cli._play_video(
        cfg, "series", "tt1", "T", _gopts(), auto=True, next_label=None, on_save=None,
        reselect_on_wrong_audio=False,
    )  # fmt: skip
    assert got["c"] is foreign and len(calls) == 1  # proceeded, no reselection
    assert "nessuna traccia audio" in capsys.readouterr().err


def test_play_video_guard_reselects_for_primary(monkeypatch):
    # Best pick lacks the primary language; a next-best candidate has it → switch to it.
    top: Stream = {"url": "u1", "name": "x\n1080p"}
    better: Stream = {"url": "u2", "name": "y\n1080p"}
    monkeypatch.setattr(cli.api, "streams", lambda *a: [top, better])
    monkeypatch.setattr(cli, "_pick_stream", lambda *a, **k: top)
    monkeypatch.setattr(cli, "_audio_langs_of", lambda cfg, ch: {"eng"})  # top has no ita
    monkeypatch.setattr(cli, "_reselect_for_primary", lambda *a, **k: better)
    got = {}
    monkeypatch.setattr(
        cli, "_play_on_mpv",
        lambda cfg, chosen, work_dir, **k: got.update(c=chosen, safety=k.get("safety_sub_lang")) or (1.0, 2.0, False),
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"], history_enabled=False)
    cli._play_video(cfg, "movie", "tt1", "T", _gopts(), auto=True, next_label=None, on_save=None)
    assert got["c"] is better and got["safety"] is None  # switched source, no subtitle net


def test_play_video_guard_safety_subtitles(monkeypatch, capsys):
    # Audio only in a fallback language (eng), not the primary (ita), and no better source:
    # play it but turn on primary-language safety subtitles.
    chosen: Stream = {"url": "u1", "name": "x\n1080p"}
    monkeypatch.setattr(cli.api, "streams", lambda *a: [chosen])
    monkeypatch.setattr(cli, "_pick_stream", lambda *a, **k: chosen)
    monkeypatch.setattr(cli, "_audio_langs_of", lambda cfg, ch: {"eng"})  # fallback only
    monkeypatch.setattr(cli, "_reselect_for_primary", lambda *a, **k: None)
    got = {}
    monkeypatch.setattr(
        cli, "_play_on_mpv",
        lambda cfg, chosen, work_dir, **k: got.update(c=chosen, safety=k.get("safety_sub_lang")) or (1.0, 2.0, False),
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"], history_enabled=False)
    cli._play_video(cfg, "movie", "tt1", "T", _gopts(), auto=True, next_label=None, on_save=None)
    assert got["c"] is chosen and got["safety"] == "ita"
    assert "sottotitoli ita attivati" in capsys.readouterr().err


def test_run_explain_movie(monkeypatch, capsys):
    # --explain ranks and prints WHY, without playing/casting.
    meta = {"id": "tt1", "type": "movie", "name": "Dune"}
    monkeypatch.setattr(cli.api, "search", lambda cfg, q: [meta])
    monkeypatch.setattr(cli, "fzf", lambda items, prompt, **k: meta)
    monkeypatch.setattr(cli.api, "streams", lambda cfg, typ, vid: [
        {"name": "[RD+] Torrentio\n4k", "title": "Dune.2024.2160p.BluRay.HEVC.ITA-GRP\n👤 9 💾 20 GB", "url": "u"},
    ])  # fmt: skip
    monkeypatch.setattr(cli.explain.tracks, "probe_tracks", lambda url: cli.tracks.Tracks())
    rc = cli.run_explain(Config(torrentio_base="tb"), "dune")
    out = capsys.readouterr().out
    assert rc == 0
    assert "--explain · Dune" in out
    assert "profilo LOCALE" in out and "profilo CAST" in out


def test_dispatch_explain_requires_query(monkeypatch):
    args = argparse.Namespace(cont=False, browse=None, query=[], explain=True)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    monkeypatch.setattr(cli, "_clear", lambda: None)
    assert cli._dispatch(Config(torrentio_base="tb"), args, opts) == 2


def test_display_title_movie():
    assert cli.display_title("Dune", None) == "Dune"


def test_display_title_series_with_name():
    v = Video(season=1, episode=3, name="Ep")
    assert cli.display_title("Show", v) == "Show · S01E03 · Ep"


def test_display_title_series_without_name():
    assert cli.display_title("Show", Video(season=2, episode=10)) == "Show · S02E10"


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


# --- terminal clear --------------------------------------------------------


def test_clear_noop_without_tty(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    cli._clear()
    assert capsys.readouterr().out == ""  # never clears a non-interactive stream


def test_clear_emits_escape_on_tty(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    cli._clear()
    assert "\x1b[2J" in capsys.readouterr().out


# --- release date + labels -------------------------------------------------


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


def test_meta_label_has_type_glyph():
    movie = cli.meta_label(Meta(type="movie", name="X", releaseInfo="2000"))
    series = cli.meta_label(Meta(type="series", name="Y", releaseInfo="2000"))
    assert cli.ui.PORTABLE.movie in movie  # portable default in tests (no Nerd Font)
    assert cli.ui.PORTABLE.series in series


def test_episode_label_format():
    label = cli.episode_label(Video(season=1, episode=3, name="Pilot"))
    assert "S01E03" in label and "Pilot" in label


def test_history_label_has_progress_bar():
    e = HistoryEntry(title="Dune", type="movie", position=50.0, duration=100.0)
    label = cli.history_label(e)
    assert "50%" in label
    assert "█" in label  # progress bar rendered


def test_history_label_no_bar_without_duration():
    label = cli.history_label(HistoryEntry(title="Dune", type="movie", duration=0.0))
    assert "%" not in label and "█" not in label


def test_meta_preview_token():
    assert cli._meta_preview(Meta(id="tt1", type="movie")) == "title movie tt1"
    assert cli._meta_preview(Meta(type="movie")) is None  # no id → no preview


def test_entry_preview_token():
    series = HistoryEntry(type="series", series_id="tt9", season=2, episode=5, video_id="v")
    assert cli._entry_preview(series) == "episode tt9 2 5"
    assert cli._entry_preview(HistoryEntry(type="movie", video_id="tt3")) == "title movie tt3"


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
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=True, autoplay=False
    )
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
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, advance = cli._play_video(
        cfg, "movie", "tt1", "Movie", opts, auto=True, next_label=None, on_save=None
    )
    assert (notice, advance) == (None, False)


def test_play_video_no_streams_returns_notice(monkeypatch):
    """No streams → return a user-facing notice (surfaced as the menu header)."""
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [])
    monkeypatch.setattr(cli.api, "meta", lambda *a, **k: {})  # released unknown → generic
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
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

    def fake(
        cfg, typ, video_id, title, opts, *, auto, next_label, on_save, reselect_on_wrong_audio=True
    ):
        calls.append({"video_id": video_id, "auto": auto, "next_label": next_label})
        idx = len(calls)  # 1-based
        # Mimic real play(): advancing requires the overlay, which requires a next_label.
        return (None, next_label is not None and idx < advance_until)

    monkeypatch.setattr(cli, "_play_video", fake)
    return calls


def test_binge_advances_then_stops(monkeypatch):
    eps = _episodes(4)
    calls = _stub_play_video(monkeypatch, advance_until=3)  # advance after ep1, ep2
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=True
    )
    assert cli._play_series(CFG, "tt", "Show", eps, eps[0], opts) is None
    assert [c["video_id"] for c in calls] == ["tt:1", "tt:2", "tt:3"]
    # first episode honours opts.auto (False); binge episodes force auto=True
    assert [c["auto"] for c in calls] == [False, True, True]


def test_binge_stops_at_last_episode(monkeypatch):
    eps = _episodes(2)
    calls = _stub_play_video(monkeypatch, advance_until=99)  # always wants to advance
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=True
    )
    cli._play_series(CFG, "tt", "Show", eps, eps[0], opts)
    assert len(calls) == 2  # no episode 3 to go to
    assert calls[-1]["next_label"] is None  # last episode offers no "next"


def test_binge_no_next_label_when_autoplay_off(monkeypatch):
    eps = _episodes(3)
    calls = _stub_play_video(monkeypatch, advance_until=99)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    cli._play_series(CFG, "tt", "Show", eps, eps[0], opts)
    assert len(calls) == 1  # advance never offered → plays one and stops
    assert calls[0]["next_label"] is None


def test_binge_starts_from_chosen_episode(monkeypatch):
    eps = _episodes(4)
    calls = _stub_play_video(monkeypatch, advance_until=99)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=True
    )
    cli._play_series(CFG, "tt", "Show", eps, eps[2], opts)  # start at E3
    assert [c["video_id"] for c in calls] == ["tt:3", "tt:4"]


def test_binge_stops_and_propagates_notice(monkeypatch):
    """An episode with no streams stops the binge and surfaces its notice."""
    eps = _episodes(3)

    def fake(*a, **k):
        return ("nessuno stream disponibile per «E1»", False)

    monkeypatch.setattr(cli, "_play_video", fake)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=True
    )
    notice = cli._play_series(CFG, "tt", "Show", eps, eps[0], opts)
    assert notice == "nessuno stream disponibile per «E1»"


# --- navigation: back-to-list + home menu ----------------------------------


def _fzf_script(returns):
    """A stub fzf_key that yields `returns` in order and records the headers it saw.
    Each item is a (key, value) tuple (key "" = Enter, "tab" = the override) or None."""
    seen = {"headers": [], "i": 0}

    def fake(items, prompt, *, header=None, expect=("tab",), preview=None):
        seen["headers"].append(header)
        val = returns[seen["i"]]
        seen["i"] += 1
        return val

    return fake, seen


def test_pick_meta_loops_until_esc_and_threads_header(monkeypatch):
    """_pick_meta replays the list after a pick (back-to-list) and shows the
    playback notice as the next header; ESC (None) leaves with rc 0."""
    items = [("Dune", {"id": "tt1", "type": "movie", "name": "Dune"})]
    fake_fzf, seen = _fzf_script([("", items[0][1]), None])  # pick once, then ESC
    monkeypatch.setattr(cli, "fzf_key", fake_fzf)
    monkeypatch.setattr(cli, "play_meta", lambda *a, **k: "non ancora disponibile")
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli._pick_meta(items, CFG, opts) == 0
    # First render shows the Tab/Alt-C hint; after the pick the notice is threaded through.
    assert seen["headers"] == [
        "Tab: avvia al volo  ·  Alt-C: casta sul TV",
        "non ancora disponibile",
    ]


def test_pick_meta_tab_flips_auto(monkeypatch):
    """Tab on a movie title flips the default (auto) to manual for that pick."""
    items = [("Dune", {"id": "tt1", "type": "movie", "name": "Dune"})]
    fake_fzf, _ = _fzf_script([("tab", items[0][1]), None])
    monkeypatch.setattr(cli, "fzf_key", fake_fzf)
    seen_auto = {}
    monkeypatch.setattr(cli, "play_meta", lambda c, m, o: seen_auto.setdefault("auto", o.auto))
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    cli._pick_meta(items, CFG, opts)
    assert seen_auto["auto"] is False  # Tab flipped auto→manual


def test_run_home_dispatches_actions(monkeypatch):
    """Home menu routes search/browse/settings then exits on ESC."""
    actions = [
        ("", (cli._SEARCH, "")),
        ("", (cli._BROWSE, "popolari")),
        ("", (cli._SETTINGS, "")),
        None,
    ]
    fake_fzf, _ = _fzf_script(actions)
    monkeypatch.setattr(cli, "fzf_key", fake_fzf)
    monkeypatch.setattr(cli, "input", lambda *a: "matrix", raising=False)
    called = {"search": 0, "browse": [], "settings": 0}
    monkeypatch.setattr(cli, "run_search", lambda c, q, o: called.__setitem__("search", q))
    monkeypatch.setattr(cli, "run_browse", lambda c, cat, o: called["browse"].append(cat))
    monkeypatch.setattr(cli.settings, "run_settings", lambda c: called.__setitem__("settings", 1))
    monkeypatch.setattr(cli, "load", lambda: CFG)
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli.run_home(CFG, opts) == 0
    assert called["search"] == "matrix"
    assert called["browse"] == [cli.CAT_MAP["popolari"]]
    assert called["settings"] == 1


def test_pick_hint_reflects_default(monkeypatch):
    auto = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    manual = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert "sorgente" in cli._pick_hint(auto)
    assert "volo" in cli._pick_hint(manual)


# --- pre-play audio/subtitle track menu ------------------------------------

from nstream.tracks import Track, Tracks  # noqa: E402

_TR = Tracks(
    audio=[Track(id=1, lang="eng", codec="aac"), Track(id=2, lang="ita", codec="eac3")],
    subs=[Track(id=1, lang="eng", codec="subrip")],
)


def _seq_fzf(monkeypatch, returns):
    """Stub cli.fzf to return successive scripted values across calls."""
    it = iter(returns)
    monkeypatch.setattr(cli, "fzf", lambda *a, **k: next(it))


def test_choose_tracks_empty_when_no_probe(monkeypatch):
    monkeypatch.setattr(cli.tracks, "probe_tracks", lambda *a, **k: Tracks())
    assert cli.choose_tracks(CFG, "http://u", "movie", "id", "/tmp") == (None, None, ())


def test_choose_tracks_pick_audio_then_play(monkeypatch):
    monkeypatch.setattr(cli.tracks, "probe_tracks", lambda *a, **k: _TR)
    # main: pick Audio → submenu: pick track id 2 → main: pick ▶ Avvia
    captured = {}

    def fzf(items, prompt, *, header=None):
        captured["last"] = items
        if prompt == "riproduzione> " and "play" not in captured:
            captured["play"] = False
            return items[1][1]  # 🔊 Audio
        if prompt == "audio> ":
            return items[2][1]  # track id 2 (after "automatico")
        return items[0][1]  # ▶ Avvia

    monkeypatch.setattr(cli, "fzf", fzf)
    assert cli.choose_tracks(CFG, "http://u", "movie", "id", "/tmp") == (2, None, ())


def test_choose_tracks_esc_returns_none(monkeypatch):
    monkeypatch.setattr(cli.tracks, "probe_tracks", lambda *a, **k: _TR)
    _seq_fzf(monkeypatch, [None])  # ESC on the main screen
    assert cli.choose_tracks(CFG, "http://u", "movie", "id", "/tmp") is None


def test_choose_tracks_subs_none(monkeypatch):
    monkeypatch.setattr(cli.tracks, "probe_tracks", lambda *a, **k: _TR)

    def fzf(items, prompt, *, header=None):
        if prompt == "riproduzione> " and not hasattr(fzf, "seen"):
            fzf.seen = True
            return items[2][1]  # 💬 Sottotitoli
        if prompt == "sottotitoli> ":
            return items[0][1]  # "nessuno" → "no"
        return items[0][1]  # ▶ Avvia

    monkeypatch.setattr(cli, "fzf", fzf)
    assert cli.choose_tracks(CFG, "http://u", "movie", "id", "/tmp") == (None, "no", ())


# --- stream ranking + curation (_pick_stream) ------------------------------


def _ranked(n, *, reason=None):
    from nstream.quality import RankedStream, StreamInfo

    return [
        RankedStream({"url": f"u{i}", "name": f"S{i}"}, StreamInfo(resolution=1080), reason)
        for i in range(n)
    ]


def test_pick_stream_cap_and_show_all(monkeypatch):
    cfg = Config(torrentio_base="tb", max_streams=20)
    monkeypatch.setattr(cli.quality, "detect_caps", lambda *a, **k: cli.quality.Caps())
    playable, excluded = _ranked(25), _ranked(2, reason="camrip (cam)")
    monkeypatch.setattr(cli.quality, "rank_streams", lambda *a, **k: (playable, excluded))
    calls = []

    def fzf(items, prompt, *, header=None):
        calls.append(items)  # both menus share the "stream> " prompt now
        if len(calls) == 1:
            return items[-1][1]  # capped menu → the "↓ mostra tutti" sentinel
        return items[0][1]  # full menu → first stream

    monkeypatch.setattr(cli, "fzf", fzf)
    out = cli._pick_stream(cfg, [{"url": "x"}] * 27, auto=False)
    # Capped menu = 20 streams + 1 "show all" entry; full menu = 25 playable + 2 excluded.
    assert len(calls[0]) == 21
    assert "mostra tutti" in calls[0][-1][0]
    assert len(calls[1]) == 27
    assert out is playable[0].stream


def test_pick_stream_auto_picks_best(monkeypatch):
    cfg = Config(torrentio_base="tb")
    monkeypatch.setattr(cli.quality, "detect_caps", lambda *a, **k: cli.quality.Caps())
    playable = _ranked(3)
    monkeypatch.setattr(cli.quality, "rank_streams", lambda *a, **k: (playable, []))
    assert cli._pick_stream(cfg, [{"url": "x"}], auto=True) is playable[0].stream


def test_pick_stream_cast_uses_cast_caps_and_audio(monkeypatch):
    """In cast mode rank against the Chromecast profile (not the laptop GPU) and pass
    cast_audio=True so the receiver-incompatible audio is filtered."""
    cfg = Config(torrentio_base="tb")

    def boom(*a, **k):
        raise AssertionError("detect_caps (GPU) must not be used when casting")

    monkeypatch.setattr(cli.quality, "detect_caps", boom)
    sentinel = cli.quality.Caps()
    monkeypatch.setattr(cli.quality, "cast_caps", lambda: sentinel)
    seen = {}
    playable = _ranked(2)

    def fake_rank(streams, caps, spec):
        seen["caps"] = caps
        seen["cast_audio"] = spec.cast_audio
        return (playable, [])

    monkeypatch.setattr(cli.quality, "rank_streams", fake_rank)
    assert cli._pick_stream(cfg, [{"url": "x"}], auto=True, cast=True) is playable[0].stream
    assert seen["caps"] is sentinel and seen["cast_audio"] is True


# --- _play_video flow wiring -----------------------------------------------


def test_play_video_auto_skips_track_menu(monkeypatch):
    """--play / binge (auto=True) must NOT open the pre-play track menu."""
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, False))

    def boom(*a, **k):
        raise AssertionError("choose_tracks must not be called when auto")

    monkeypatch.setattr(cli, "choose_tracks", boom)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, _ = cli._play_video(
        cfg, "movie", "tt1", "M", opts, auto=True, next_label=None, on_save=None
    )
    assert notice is None


def test_play_video_interactive_calls_track_menu(monkeypatch):
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(cli, "_pick_stream", lambda *a, **k: {"url": "http://u", "name": "S"})
    monkeypatch.setattr(cli, "choose_tracks", lambda *a, **k: (2, 1, ()))
    seen = {}
    monkeypatch.setattr(
        cli,
        "play",
        lambda *a, **k: (
            seen.update(aid=k.get("audio_id"), sid=k.get("sub_id")) or (0.0, 0.0, False)
        ),
    )
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    cli._play_video(cfg, "movie", "tt1", "M", opts, auto=False, next_label=None, on_save=None)
    assert seen == {"aid": 2, "sid": 1}


def test_play_video_cast_branch_no_track_menu(monkeypatch):
    """In cast mode choose_tracks is never called; cast() gets the resolved device."""
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(cli, "_pick_stream", lambda *a, **k: {"url": "http://u", "name": "S"})
    monkeypatch.setattr(cli, "_resolve_device", lambda c, **k: "TV")

    def boom(*a, **k):
        raise AssertionError("choose_tracks must not run in cast mode")

    monkeypatch.setattr(cli, "choose_tracks", boom)
    seen = {}
    monkeypatch.setattr(
        cli, "cast",
        lambda *a, **k: seen.update(device=k.get("device")) or (0.0, 0.0, False),
    )  # fmt: skip
    opts = cli.PlayOpts(
        auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, _ = cli._play_video(
        cfg, "movie", "tt1", "M", opts, auto=True, next_label=None, on_save=None
    )
    assert notice is None
    assert seen == {"device": "TV"}


def test_play_video_cast_unavailable_falls_back_to_local(monkeypatch):
    """No Chromecast reachable → degrade to local mpv instead of failing."""
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(cli, "_pick_stream", lambda *a, **k: {"url": "http://u", "name": "S"})

    def boom(_cfg, **k):
        raise cli.CastUnavailable("nessun Chromecast in rete")

    monkeypatch.setattr(cli, "_resolve_device", boom)
    monkeypatch.setattr(cli, "choose_tracks", lambda *a, **k: (None, None, ()))

    def no_cast(*a, **k):
        raise AssertionError("cast must not run when no device")

    monkeypatch.setattr(cli, "cast", no_cast)
    seen = {}
    monkeypatch.setattr(cli, "play", lambda *a, **k: seen.update(local=True) or (0.0, 0.0, ""))
    opts = cli.PlayOpts(
        auto=False, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, advance = cli._play_video(
        cfg, "movie", "tt1", "M", opts, auto=False, next_label=None, on_save=None
    )
    assert seen.get("local") is True and advance is False


def test_play_video_local_to_cast_on_signal(monkeypatch):
    """Alt-C in mpv (play() returns 'cast') re-casts from the current position."""
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(cli, "_pick_stream", lambda *a, **k: {"url": "http://u", "name": "S"})
    monkeypatch.setattr(cli, "choose_tracks", lambda *a, **k: (None, None, ()))
    monkeypatch.setattr(cli.shutil, "which", lambda _x: "/usr/bin/catt")
    monkeypatch.setattr(cli, "play", lambda *a, **k: (55.0, 100.0, "cast"))
    monkeypatch.setattr(cli, "_resolve_device", lambda c, **k: "TV")
    seen = {}
    monkeypatch.setattr(
        cli,
        "cast",
        lambda *a, **k: (
            seen.update(start=k.get("start"), device=k.get("device")) or (55.0, 100.0, False)
        ),
    )
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, advance = cli._play_video(
        cfg, "movie", "tt1", "M", opts, auto=False, next_label=None, on_save=None
    )
    assert seen == {"start": 55.0, "device": "TV"} and advance is False


# --- cast stream selection (language switch) --------------------------------

from nstream.config import Stream as _Stream  # noqa: E402

_S_ITA: _Stream = {
    "url": "http://ita",
    "name": "[RD+] Torrentio\n1080p",
    "title": "Film.2020.iTA.1080p.BluRay.DDP5.1.x264-GRP\n👤 20 💾 8.0 GB ⚙️ x",
}
_S_ENG_REMUX: _Stream = {
    "url": "http://eng-remux",
    "name": "[RD+] Torrentio\n4k",
    "title": "Film.2020.ENG.2160p.UHD.BluRay.REMUX.TrueHD-GRP\n👤 30 💾 60.0 GB ⚙️ x",
}
_S_ENG_WEBDL: _Stream = {
    "url": "http://eng-webdl",
    "name": "[RD+] Torrentio\n1080p",
    "title": "Film.2020.ENG.1080p.WEB-DL.DDP5.1.x264-GRP\n👤 10 💾 6.0 GB ⚙️ x",
}


def test_cast_languages_lists_compatible(monkeypatch):
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    langs = cli._cast_languages(cfg, [_S_ITA, _S_ENG_REMUX, _S_ENG_WEBDL])
    assert langs == ("ita", "eng")  # preferred order; eng present via the WEB-DL


def test_cast_resolver_picks_compatible_release(monkeypatch):
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    resolve = cli._cast_resolver(cfg, [_S_ITA, _S_ENG_REMUX, _S_ENG_WEBDL])
    # ITA → the ITA release; ENG → the WEB-DL, never the TrueHD remux; missing → None
    assert resolve("ita") == "http://ita"
    assert resolve("eng") == "http://eng-webdl"
    assert resolve("ger") is None


# --- leaf-list keys (Tab / Alt-C) ------------------------------------------


def test_apply_key_alt_c_casts():
    base = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    out = cli._apply_key(base, "alt-c")
    assert out.cast is True and out.cast_choose is True
    assert cli._apply_key(base, "tab").auto is False  # Tab still flips
    assert cli._apply_key(base, "").auto is True  # Enter keeps default


def test_pick_meta_alt_c_sets_cast(monkeypatch):
    items = [("Dune", {"id": "tt1", "type": "movie", "name": "Dune"})]
    fake_fzf, _ = _fzf_script([("alt-c", items[0][1]), None])
    monkeypatch.setattr(cli, "fzf_key", fake_fzf)
    seen = {}
    monkeypatch.setattr(
        cli, "play_meta", lambda c, m, o: seen.update(cast=o.cast, choose=o.cast_choose)
    )
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    cli._pick_meta(items, CFG, opts)
    assert seen == {"cast": True, "choose": True}
