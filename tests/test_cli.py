"""Unit tests for cli orchestration: pickers, the play-flow wiring, and navigation.

Low-level playback/cast/picker units live in test_player.py, test_caster.py and
test_picker.py; here we exercise cli's coordination, mocking play/cast/_resolve_device
(re-exported from those modules) as cli globals."""

from __future__ import annotations

import argparse
import json

import pytest

from nstream import cli
from nstream.config import Config, HistoryEntry, Meta

CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])


def test_main_preview_dispatch(monkeypatch):
    # `nstream __preview …` is handled before argparse and forwarded to preview.run_preview.
    seen = {}
    monkeypatch.setattr(cli.sys, "argv", ["nstream", "__preview", "title", "movie", "tt1"])
    monkeypatch.setattr(cli.preview, "run_preview", lambda argv: (seen.update(argv=argv), 0)[1])
    assert cli.main() == 0
    assert seen["argv"] == ["title", "movie", "tt1"]


def test_main_layout_dispatch(monkeypatch):
    # `nstream __layout` (fzf's resize transform) is forwarded to preview.run_layout.
    monkeypatch.setattr(cli.sys, "argv", ["nstream", "__layout"])
    monkeypatch.setattr(cli.preview, "run_layout", lambda: 0)
    assert cli.main() == 0


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
    args = argparse.Namespace(
        cont=False, browse=None, query=[], explain=True, json=False, movies=False, series=False
    )
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    monkeypatch.setattr(cli, "_clear", lambda: None)
    assert cli._dispatch(Config(torrentio_base="tb"), args, opts) == 2


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


# --- terminal clear --------------------------------------------------------


def test_clear_noop_without_tty(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    cli._clear()
    assert capsys.readouterr().out == ""  # never clears a non-interactive stream


def test_clear_emits_escape_on_tty(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    cli._clear()
    assert "\x1b[2J" in capsys.readouterr().out


# --- preview tokens --------------------------------------------------------


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


def test_play_video_cast_fallback_selects_local_profile(monkeypatch):
    """Regression: when --cast resolves no device, stream selection must run with the
    LOCAL profile — not the Chromecast caps (which e.g. drop AV1 as "no-HW" even though
    the local GPU decodes it). The device is resolved BEFORE selection for this reason."""
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(cli, "_resolve_cast_device", lambda *a, **k: None)  # no TV on the LAN
    monkeypatch.setattr(cli, "play", lambda *a, **k: (10.0, 100.0, False))
    seen = {}

    def spy(cfg, results, opts, **kw):
        seen["cast"] = opts.cast
        return cli.stream_select.VettedStream(stream=results[0], auto=True, safety_sub_lang=None)

    monkeypatch.setattr(cli.stream_select, "prepare_stream", spy)
    opts = cli.PlayOpts(
        auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, advance = cli._play_video(
        cfg, "movie", "tt1", "M", opts, auto=True, next_label=None, on_save=None
    )
    assert seen["cast"] is False  # selection downgraded to the local profile
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


# --- series dispatch (the flow itself lives in series.py / test_series.py) --


def test_play_meta_series_dispatches_to_series(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.series, "play", lambda cfg, meta, opts, **kw: seen.update(meta=meta, kw=kw) or "hdr"
    )
    meta = Meta(id="tt9", type="series", name="Show")
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=True
    )
    assert cli.play_meta(CFG, meta, opts) == "hdr"
    assert seen["meta"] is meta
    # cli injects its own leaf-list helpers + the player entry point.
    assert seen["kw"]["pick_hint"] is cli._pick_hint
    assert seen["kw"]["apply_key"] is cli._apply_key
    assert callable(seen["kw"]["play_video"])


def test_play_meta_movie_skips_series(monkeypatch):
    monkeypatch.setattr(cli.series, "play", lambda *a, **k: pytest.fail("series.play called"))
    monkeypatch.setattr(cli, "_play_video", lambda *a, **k: ("notice", False))
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli.play_meta(CFG, Meta(id="tt1", type="movie", name="Dune"), opts) == "notice"


def test_play_history_series_dispatches_to_resume(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.series, "resume", lambda cfg, entry, opts, **kw: seen.update(entry=entry) or None
    )
    entry = HistoryEntry(video_id="tt9:1:2", type="series", title="Show", series_id="tt9")
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=True, autoplay=True
    )
    assert cli.play_history(CFG, entry, opts) is None
    assert seen["entry"] is entry


def test_play_history_movie_plays_directly(monkeypatch):
    seen = {}

    def fake(cfg, typ, video_id, title, opts, **kw):
        seen.update(typ=typ, video_id=video_id, title=title)
        return (None, False)

    monkeypatch.setattr(cli, "_play_video", fake)
    monkeypatch.setattr(cli.series, "resume", lambda *a, **k: pytest.fail("series.resume called"))
    entry = HistoryEntry(video_id="tt3", type="movie", title="Dune")
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli.play_history(CFG, entry, opts) is None
    assert seen == {"typ": "movie", "video_id": "tt3", "title": "Dune"}


def test_series_player_binds_cfg_and_type(monkeypatch):
    """The injected callable is _play_video with cfg and typ="series" pre-bound."""
    seen = {}

    def fake(cfg, typ, video_id, title, opts, **kw):
        seen.update(cfg=cfg, typ=typ, video_id=video_id, kw=kw)
        return ("n", True)

    monkeypatch.setattr(cli, "_play_video", fake)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=True
    )
    play_video = cli._series_player(CFG)
    out = play_video(
        "tt9:1:1", "Show 1x01", opts, auto=False, next_label="nxt", on_save=lambda p, d: None
    )
    assert out == ("n", True)
    assert seen["cfg"] is CFG and seen["typ"] == "series" and seen["video_id"] == "tt9:1:1"
    assert seen["kw"]["next_label"] == "nxt" and seen["kw"]["auto"] is False


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
    """Home menu routes search (mixed), the typed sections and settings, then ESC exits."""
    actions = [
        ("", (cli._SEARCH, "")),
        ("", (cli._SECTION, "series")),
        ("", (cli._SETTINGS, "")),
        None,
    ]
    fake_fzf, _ = _fzf_script(actions)
    monkeypatch.setattr(cli, "fzf_key", fake_fzf)
    monkeypatch.setattr(cli, "input", lambda *a: "matrix", raising=False)
    called: dict[str, object] = {}
    sections: list[str] = []
    monkeypatch.setattr(
        cli, "run_search", lambda c, q, o, typ=None: called.__setitem__("search", (q, typ))
    )
    monkeypatch.setattr(cli, "run_section", lambda c, typ, o: sections.append(typ))
    monkeypatch.setattr(cli.settings, "run_settings", lambda c: called.__setitem__("settings", 1))
    monkeypatch.setattr(cli, "load", lambda: CFG)
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli.run_home(CFG, opts) == 0
    assert called["search"] == ("matrix", None)  # the home search stays mixed
    assert sections == ["series"]
    assert called["settings"] == 1


def test_run_home_shows_typed_sections_not_mixed_browse(monkeypatch):
    """The home offers the Film / Serie TV sections instead of the old mixed catalog rows."""
    seen = {}

    def fake(items, prompt, *, header=None, preview=None):
        seen["values"] = [v for _, v in items]
        seen["labels"] = [label for label, _ in items]
        seen["prompt"] = prompt
        return None  # ESC

    monkeypatch.setattr(cli, "fzf_key", fake)
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli.run_home(CFG, opts) == 0
    assert seen["prompt"] == "nstream> "
    assert (cli._SECTION, "movie") in seen["values"]
    assert (cli._SECTION, "series") in seen["values"]
    assert not any(v[0] == cli._BROWSE for v in seen["values"])  # catalogs live in the sections
    labels = " ".join(seen["labels"])
    assert "Film" in labels and "Serie TV" in labels and "Popolari" not in labels


def test_run_section_series_filters_recent_and_types_actions(monkeypatch):
    """A section menu shows the type-filtered continue-watching and routes typed
    search/browse; its prompt names the section."""
    recent_typs = []
    entry = HistoryEntry(video_id="tt9:1:1", type="series", title="Show", ts=1.0)

    def fake_recent(cfg, limit=30, typ=None):
        recent_typs.append(typ)
        return [entry]

    monkeypatch.setattr(cli.state, "recent", fake_recent)
    actions = [("", (cli._BROWSE, "popolari")), ("", (cli._SEARCH, "")), None]
    it = iter(actions)
    prompts = []

    def fake_fzf(items, prompt, *, header=None, preview=None):
        prompts.append(prompt)
        assert items[0][1] is entry  # the filtered continue-watching row leads the menu
        return next(it)

    monkeypatch.setattr(cli, "fzf_key", fake_fzf)
    monkeypatch.setattr(cli, "input", lambda *a: "fargo", raising=False)
    called = {}
    monkeypatch.setattr(
        cli, "run_browse", lambda c, cat, o, typ=None: called.__setitem__("browse", (cat, typ))
    )
    monkeypatch.setattr(
        cli, "run_search", lambda c, q, o, typ=None: called.__setitem__("search", (q, typ))
    )
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=True, autoplay=False
    )
    assert cli.run_section(CFG, "series", opts) == 0
    assert recent_typs == ["series"] * 3  # one per menu render
    assert prompts == ["serie> "] * 3
    assert called["browse"] == (cli.CAT_MAP["popolari"], "series")
    assert called["search"] == ("fargo", "series")


def test_run_browse_typed_uses_catalog(monkeypatch):
    """With a type, run_browse goes through api.catalog (single-type); without, api.browse."""
    seen = {}
    monkeypatch.setattr(
        cli.api, "catalog", lambda c, typ, cat: seen.setdefault("catalog", (typ, cat)) and []
    )
    monkeypatch.setattr(cli.api, "browse", lambda c, cat: seen.setdefault("browse", cat) and [])
    monkeypatch.setattr(cli, "_pick_meta", lambda items, c, o: 0)
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    cli.run_browse(CFG, "top", opts, typ="series")
    assert seen == {"catalog": ("series", "top")}  # api.browse untouched
    seen.clear()
    cli.run_browse(CFG, "top", opts)
    assert seen == {"browse": "top"}  # default stays mixed


def test_run_search_forwards_type(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli.api, "search", lambda c, q, typ=None: seen.setdefault("call", (q, typ)) and []
    )
    monkeypatch.setattr(cli, "_pick_meta", lambda items, c, o: 0)
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    cli.run_search(CFG, "fargo", opts, typ="movie")
    assert seen["call"] == ("fargo", "movie")


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
    monkeypatch.setattr(
        cli.stream_select,
        "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: cli.stream_select.VettedStream(
            {"url": "http://u", "name": "S"}, auto, None
        ),
    )
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
    monkeypatch.setattr(
        cli.stream_select,
        "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: cli.stream_select.VettedStream(
            {"url": "http://u", "name": "S"}, auto, None
        ),
    )
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
    monkeypatch.setattr(
        cli.stream_select,
        "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: cli.stream_select.VettedStream(
            {"url": "http://u", "name": "S"}, auto, None
        ),
    )

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
    monkeypatch.setattr(
        cli.stream_select,
        "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: cli.stream_select.VettedStream(
            {"url": "http://u", "name": "S"}, auto, None
        ),
    )
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


# --- headless mode (--json / run_auto) -------------------------------------


def _hns(**kw):
    """argparse.Namespace for a headless (--json) invocation, with safe defaults."""
    base = {
        "query": [],
        "cont": False,
        "sub_menu": False,
        "year": None,
        "season": None,
        "episode": None,
        "device": None,
        "audio_lang": None,
        "probe": False,
        "stop": False,
        "status": False,
        "browse": None,
        "volume": None,
        "follow": None,  # BooleanOptionalAction default → fire-and-return
        "movies": False,
        "series": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _hopts(cast=False):
    return cli.PlayOpts(
        auto=True, cast=cast, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )


def _VETTED(s):
    return cli.stream_select.VettedStream(stream=s, auto=True, safety_sub_lang=None)


def _wire_movie(monkeypatch, *, name="Dune", stream=None):
    stream = stream or {
        "name": "[RD+] Torrentio\n1080p",
        "title": "Dune.2024.1080p.WEB-DL.HEVC.ITA-GRP\n👤 9 💾 8 GB",
        "url": "http://rd.example/secret-token-abc/dune.mkv",
    }
    monkeypatch.setattr(
        cli.api, "search", lambda cfg, q: [{"id": "tt1", "type": "movie", "name": name}]
    )
    monkeypatch.setattr(cli.api, "streams", lambda cfg, t, v: [stream])
    monkeypatch.setattr(
        cli.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    # Keep audio-language discovery hermetic (no real rank_streams/vainfo in unit tests).
    monkeypatch.setattr(
        cli.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    monkeypatch.setattr(cli, "auto_subs", lambda *a, **k: ())
    monkeypatch.setattr(cli, "device_volume", lambda device: (0.4, False))
    return stream


def test_run_auto_movie_emits_json(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    seen = {}
    monkeypatch.setattr(cli, "play", lambda *a, **k: seen.update(played=True) or (0.0, 0.0, ""))
    rc = cli.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and seen.get("played")
    assert out["ok"] and out["action"] == "play" and out["type"] == "movie"
    assert out["title"] == "Dune" and out["selection"] == "exact"
    assert out["stream"]["resolution"] == 1080 and out["stream"]["codec"] == "hevc"
    assert out["stream"]["cached"] is True and out["stream"]["backend"] == "debrid"


def test_run_auto_skips_fzf(monkeypatch):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, ""))

    def boom(*a, **k):
        raise AssertionError("fzf called")

    monkeypatch.setattr(cli, "fzf", boom)
    monkeypatch.setattr(cli, "fzf_key", boom)
    assert cli.run_auto(CFG, _hns(query=["dune"]), _hopts()) == 0


def test_run_auto_exact_match_over_first(monkeypatch, capsys):
    metas = [
        {"id": "tt9", "type": "movie", "name": "Dune: Part Two"},
        {"id": "tt1", "type": "movie", "name": "Dune"},
    ]
    monkeypatch.setattr(cli.api, "search", lambda cfg, q: metas)
    monkeypatch.setattr(
        cli.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        cli.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(cli, "auto_subs", lambda *a, **k: ())
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, ""))
    cli.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert out["imdb_id"] == "tt1" and out["selection"] == "exact"


def test_run_auto_year_disambiguates(monkeypatch, capsys):
    metas = [
        {"id": "old", "type": "movie", "name": "Dune", "releaseInfo": "1984"},
        {"id": "new", "type": "movie", "name": "Dune", "releaseInfo": "2021"},
    ]
    monkeypatch.setattr(cli.api, "search", lambda cfg, q: metas)
    monkeypatch.setattr(
        cli.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        cli.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(cli, "auto_subs", lambda *a, **k: ())
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, ""))
    cli.run_auto(CFG, _hns(query=["dune"], year="2021"), _hopts())
    assert json.loads(capsys.readouterr().out)["imdb_id"] == "new"


def test_run_auto_no_result(monkeypatch, capsys):
    monkeypatch.setattr(cli.api, "search", lambda cfg, q: [])
    rc = cli.run_auto(CFG, _hns(query=["zzz"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["error"] == "no_result"


def test_run_auto_series_season_episode(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.api, "search", lambda cfg, q: [{"id": "tt2", "type": "series", "name": "Severance"}]
    )
    eps = [
        {"id": "tt2:1:1", "season": 1, "episode": 1},
        {"id": "tt2:1:2", "season": 1, "episode": 2},
    ]
    monkeypatch.setattr(cli.api, "episodes", lambda cfg, sid: eps)
    monkeypatch.setattr(
        cli.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        cli.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(cli, "auto_subs", lambda *a, **k: ())
    seen = {}
    monkeypatch.setattr(
        cli.api,
        "streams",
        lambda cfg, t, v: seen.update(vid=v) or [{"name": "x", "title": "y", "url": "u"}],
    )
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, ""))
    cli.run_auto(CFG, _hns(query=["severance"], season=1, episode=2), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert seen["vid"] == "tt2:1:2"
    assert out["type"] == "series" and out["season"] == 1 and out["episode"] == 2


def test_run_auto_episode_not_found(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.api, "search", lambda cfg, q: [{"id": "tt2", "type": "series", "name": "Severance"}]
    )
    monkeypatch.setattr(
        cli.api, "episodes", lambda cfg, sid: [{"id": "tt2:1:1", "season": 1, "episode": 1}]
    )
    rc = cli.run_auto(CFG, _hns(query=["severance"], season=5, episode=9), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "episode_not_found"
    assert {"season": 1, "episode": 1} in out["available"]


def test_run_auto_cast_device_not_found(monkeypatch, capsys):
    _wire_movie(monkeypatch)

    def boom(cfg, **k):
        raise cli.CastUnavailable("più dispositivi in rete")

    monkeypatch.setattr(cli, "_resolve_device", boom)
    monkeypatch.setattr(
        cli, "cast", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cast called"))
    )
    rc = cli.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "device_not_found"


def test_run_auto_cast_emits_device(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    seen = {}
    monkeypatch.setattr(
        cli, "cast", lambda *a, **k: seen.update(follow=k.get("follow")) or (0.0, 0.0, False)
    )
    cli.run_auto(CFG, _hns(query=["dune"], device="Salotto"), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "cast" and out["device"] == "Salotto"
    assert seen["follow"] is False  # default fire-and-return


def test_run_auto_cast_remux_failure_notice(monkeypatch, capsys):
    """A failed Tier-2 remux must not degrade silently: the direct-cast fallback surfaces a
    stderr warning and the JSON `notice` (first audio track may be silent/wrong-language)."""
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    plan = cli.stream_select.CastAudioPlan("remux", stream, 1, "ita", verified=True)
    monkeypatch.setattr(cli.stream_select, "vet_cast_audio", lambda *a, **k: plan)
    monkeypatch.setattr(cli.remux, "remux_for_cast", lambda *a, **k: None)  # ffmpeg failed
    seen = {}
    monkeypatch.setattr(cli, "cast", lambda *a, **k: seen.update(cast=True) or (0.0, 0.0, False))
    monkeypatch.setattr(cli.engine, "detach_spawned", lambda: seen.update(detached=True))
    rc = cli.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert rc == 0 and seen.get("cast")  # degraded to a direct cast
    assert out["notice"] and "remux non riuscito" in out["notice"]
    assert "remux non riuscito" in cap.err
    # Fire-and-return handoff leaves a spawned TorrServer alive for the ongoing cast.
    assert seen.get("detached")


def test_run_auto_no_debrid_url_leak(monkeypatch, capsys):
    _wire_movie(
        monkeypatch,
        stream={
            "name": "[RD+] Torrentio\n1080p",
            "title": "Dune.2021.1080p\n💾 8 GB",
            "url": "http://rd.example/secret-token-abc123/dune.mkv",
        },
    )
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, ""))
    cli.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = capsys.readouterr().out
    assert "secret-token-abc123" not in out and "http" not in out


def test_run_auto_sub_menu_rejected(monkeypatch, capsys):
    rc = cli.run_auto(CFG, _hns(query=["dune"], sub_menu=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["error"] == "usage"


def test_dispatch_json_skips_clear(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_clear", lambda: seen.update(cleared=True))
    monkeypatch.setattr(cli, "run_auto", lambda cfg, args, opts: 0)
    cli._dispatch(CFG, _hns(query=["dune"], browse=None, explain=False, json=True), _hopts())
    assert "cleared" not in seen


def test_run_auto_enriched_json_audio_fields(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, ""))
    cli.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert out["available_audio"] == ["ita", "eng"]
    assert out["audio_lang"] == CFG.primary  # no --audio-lang → expected/primary


def test_run_auto_audio_lang_forces_dub(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    seen = {}

    def pick(cfg, results, lang, *, cast, probe_cap=4):
        seen["lang"] = lang
        return results[0], True  # (stream, verified) — track-accurate confirmed

    monkeypatch.setattr(cli.stream_select, "pick_audio_stream_verified", pick)
    # prepare_stream must NOT be used on the forced-audio path.
    monkeypatch.setattr(
        cli.stream_select, "prepare_stream",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prepare_stream used")),
    )  # fmt: skip
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, ""))
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, audio_lang="eng",
    )  # fmt: skip
    rc = cli.run_auto(CFG, _hns(query=["dune"], audio_lang="eng"), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and seen["lang"] == "eng"
    assert out["audio_lang"] == "eng"


def test_run_auto_audio_lang_unavailable(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        cli.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    monkeypatch.setattr(
        cli, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, audio_lang="jpn",
    )  # fmt: skip
    rc = cli.run_auto(CFG, _hns(query=["dune"], audio_lang="jpn"), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "audio_lang_unavailable"
    assert out["available_audio"] == ["ita", "eng"]


def test_run_auto_probe_lists_audio_and_subs(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.api, "search", lambda cfg, q: [{"id": "tt1", "type": "movie", "name": "Dune"}]
    )
    monkeypatch.setattr(
        cli.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        cli.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    monkeypatch.setattr(cli, "available_subtitle_langs", lambda cfg, t, v: ["eng", "fre", "ita"])
    monkeypatch.setattr(
        cli, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    rc = cli.run_auto(CFG, _hns(query=["dune"], probe=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "probe"
    assert out["available_audio"] == ["ita", "eng"]
    assert out["available_subtitles"] == ["eng", "fre", "ita"]


def test_run_auto_cast_reports_volume(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cli, "cast", lambda *a, **k: (0.0, 0.0, False))
    monkeypatch.setattr(cli, "device_volume", lambda device: (0.4, False))
    cli.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["volume"] == 0.4 and out["muted"] is False and out["notice"] is None


def test_run_auto_cast_warns_volume_zero(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cli, "cast", lambda *a, **k: (0.0, 0.0, False))
    monkeypatch.setattr(cli, "device_volume", lambda device: (0.0, False))
    cli.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["volume"] == 0.0 and out["notice"] and "volume" in out["notice"].lower()


# --- completion: stop / status / browse / volume / track-accurate ----------


def test_run_auto_stop(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cli.caster, "stop", lambda device: True)
    rc = cli.run_auto(CFG, _hns(stop=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "stop" and out["device"] == "192.168.1.5"


def test_run_auto_status(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        cli.caster, "status",
        lambda device: {"player_state": "PLAYING", "title": "X", "position": 12.0,
                        "duration": 100.0, "volume": 0.4, "muted": False},
    )  # fmt: skip
    rc = cli.run_auto(CFG, _hns(status=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "status" and out["player_state"] == "PLAYING"
    assert out["title"] == "X" and out["volume"] == 0.4


def test_run_auto_stop_device_not_found(monkeypatch, capsys):
    def boom(cfg, **k):
        raise cli.CastUnavailable("nessun Chromecast")

    monkeypatch.setattr(cli, "_resolve_device", boom)
    rc = cli.run_auto(CFG, _hns(stop=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "device_not_found"


def test_run_auto_browse(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.api, "browse", lambda cfg, cat: [{"id": "tt1", "type": "movie", "name": "Popular"}]
    )
    monkeypatch.setattr(
        cli.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        cli.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(
        cli.stream_select, "audio_languages", lambda cfg, results, *, cast: ("eng",)
    )
    monkeypatch.setattr(cli, "auto_subs", lambda *a, **k: ())
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, ""))
    rc = cli.run_auto(CFG, _hns(browse="popolari"), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["title"] == "Popular" and out["selection"] == "browse"


def test_run_auto_cast_sets_volume(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cli, "cast", lambda *a, **k: (0.0, 0.0, False))
    seen = {}
    monkeypatch.setattr(cli.caster, "set_volume", lambda device, level: seen.update(level=level))
    monkeypatch.setattr(cli, "device_volume", lambda device: (0.35, False))
    cli.run_auto(CFG, _hns(query=["dune"], volume=35), _hopts(cast=True))
    assert seen["level"] == 35


def test_run_auto_audio_lang_not_in_real_tracks(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        cli.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    # name tags claim ita, but ffprobe verification finds no candidate → reject (no wrong dub).
    monkeypatch.setattr(
        cli.stream_select, "pick_audio_stream_verified",
        lambda cfg, results, lang, *, cast, probe_cap=4: (None, False),
    )  # fmt: skip
    monkeypatch.setattr(
        cli, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, audio_lang="ita",
    )  # fmt: skip
    rc = cli.run_auto(CFG, _hns(query=["dune"], audio_lang="ita"), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "audio_lang_unavailable"


# --- headless title resolution (_select_meta) --------------------------------


def test_select_meta_want_series_prefers_series():
    # An explicit --season/--episode means a series: a same-named movie (e.g. the 2026
    # Korean film "Mr. Robot") must not shadow the series the caller asks an episode of.
    movie = Meta(id="tt2", type="movie", name="Mr. Robot")
    series = Meta(id="tt1", type="series", name="Mr. Robot")
    meta, how = cli._select_meta([movie, series], "mr robot", None, want_series=True)
    assert meta["id"] == "tt1"
    assert how == "exact"


def test_select_meta_want_series_falls_back_to_movies():
    # No series in the results → degrade to the normal pick instead of failing.
    movie = Meta(id="tt2", type="movie", name="Mr. Robot")
    meta, _ = cli._select_meta([movie], "mr robot", None, want_series=True)
    assert meta["id"] == "tt2"


def test_select_meta_default_keeps_first_exact():
    movie = Meta(id="tt2", type="movie", name="Mr. Robot")
    series = Meta(id="tt1", type="series", name="Mr. Robot")
    meta, _ = cli._select_meta([movie, series], "mr robot", None)
    assert meta["id"] == "tt2"  # no season/episode hint → existing behaviour unchanged


# --- explicit --movies/--series type flags ----------------------------------

_FARGO = [
    {"id": "ttm", "type": "movie", "name": "Fargo"},
    {"id": "tts", "type": "series", "name": "Fargo"},
]


def _wire_fargo(monkeypatch):
    """Same-title movie + series, with enough plumbing to reach the final JSON."""
    monkeypatch.setattr(cli.api, "search", lambda cfg, q: list(_FARGO))
    monkeypatch.setattr(
        cli.api, "episodes", lambda cfg, sid: [{"id": "tts:1:1", "season": 1, "episode": 1}]
    )
    monkeypatch.setattr(
        cli.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        cli.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(cli, "auto_subs", lambda *a, **k: ())
    monkeypatch.setattr(cli, "play", lambda *a, **k: (0.0, 0.0, ""))


def test_run_auto_series_flag_picks_series_over_same_title_movie(monkeypatch, capsys):
    """--series drops the same-named movie before the title match (explicit beats order)."""
    _wire_fargo(monkeypatch)
    rc = cli.run_auto(CFG, _hns(query=["fargo"], series=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["imdb_id"] == "tts" and out["type"] == "series"


def test_run_auto_movies_flag_picks_movie(monkeypatch, capsys):
    _wire_fargo(monkeypatch)
    rc = cli.run_auto(CFG, _hns(query=["fargo"], movies=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["imdb_id"] == "ttm" and out["type"] == "movie"


def test_run_auto_season_inference_unchanged_without_flags(monkeypatch, capsys):
    """No explicit flag → --season still infers the series (pre-flag behaviour)."""
    _wire_fargo(monkeypatch)
    rc = cli.run_auto(CFG, _hns(query=["fargo"], season=1, episode=1), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["imdb_id"] == "tts" and out["season"] == 1


def test_run_auto_movies_with_season_is_usage_error(monkeypatch, capsys):
    """--movies + --season/--episode is contradictory → usage error, nothing fetched."""
    monkeypatch.setattr(
        cli.api, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched"))
    )
    rc = cli.run_auto(CFG, _hns(query=["fargo"], movies=True, season=1), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["error"] == "usage" and "--movies" in out["message"]


def test_run_auto_series_flag_no_result_reflects_filter(monkeypatch, capsys):
    """When the filter empties the results, the error message names the type."""
    monkeypatch.setattr(
        cli.api, "search", lambda cfg, q: [{"id": "ttm", "type": "movie", "name": "Fargo"}]
    )
    rc = cli.run_auto(CFG, _hns(query=["fargo"], series=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "no_result" and "serie" in out["message"]


def test_main_movies_series_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["nstream", "--movies", "--series", "fargo"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 2
