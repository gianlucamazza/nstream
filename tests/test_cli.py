"""Unit tests for cli orchestration: pickers, the play-flow wiring, and navigation.

Low-level playback/cast/picker units live in test_player.py, test_caster.py and
test_picker.py; here we exercise cli's coordination, mocking play/cast/_resolve_device
(re-exported from those modules) as cli globals."""

from __future__ import annotations

import argparse
import json
import threading

import pytest

from nstream import cast_flow, cli, subs
from nstream.config import Config
from nstream.types import HistoryEntry, Meta

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
    monkeypatch.setattr(cli.explain.tracks, "probe_tracks", lambda url: Tracks())
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


def test_dispatch_warms_discovery_on_interactive_paths(monkeypatch):
    # The background device scan must start for the TUI flows (so a later cast is
    # instant) but not for --explain (never casts) or --json (scans on demand).
    started = []
    monkeypatch.setattr(cli.discovery, "start_background", lambda: started.append(1))
    monkeypatch.setattr(cli, "_clear", lambda: None)
    monkeypatch.setattr(cli, "run_home", lambda cfg, opts: 0)
    monkeypatch.setattr(cli, "run_explain", lambda cfg, query, opts=None: 0)
    monkeypatch.setattr(cli.headless, "run", lambda cfg, args, opts: 0)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )

    def ns(**kw):
        base = dict(
            cont=False, browse=None, query=[], explain=False, json=False, movies=False, series=False
        )
        return argparse.Namespace(**{**base, **kw})

    assert cli._dispatch(Config(torrentio_base="tb"), ns(), opts) == 0
    assert started == [1]  # home (interactive) warms discovery
    cli._dispatch(Config(torrentio_base="tb"), ns(explain=True, query=["x"]), opts)
    cli._dispatch(Config(torrentio_base="tb"), ns(json=True), opts)
    assert started == [1]  # --explain / --json don't


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
    notice, advance, _q = cli._play_video(
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
    notice, advance, _q = cli._play_video(
        cfg, "movie", "tt1", "M", opts, auto=True, next_label=None, on_save=None
    )
    assert seen["cast"] is False  # selection downgraded to the local profile
    assert (notice, advance) == (None, False)


def test_play_video_cast_fetch_overlaps_device_scan(monkeypatch):
    """On the cast path the stream fetch runs in a background thread, started BEFORE
    the (slow, up to ~20s) catt scan resolves the device, and joined before selection."""
    cfg = Config(torrentio_base="tb", hwdec="")
    order = []
    fetch_started = threading.Event()

    def fake_streams(*a, **k):
        order.append("fetch-start")
        fetch_started.set()
        return [{"url": "http://u", "name": "S"}]

    def fake_resolve(*a, **k):
        # The fetch must already be in flight while the scan is still resolving.
        assert fetch_started.wait(timeout=5), "stream fetch not started before device scan"
        order.append("device-resolved")
        return None  # degrade to local → plays via mocked mpv, no cast stack needed

    monkeypatch.setattr(cli.api, "streams", fake_streams)
    monkeypatch.setattr(cli, "_resolve_cast_device", fake_resolve)
    monkeypatch.setattr(cli, "play", lambda *a, **k: (10.0, 100.0, False))
    opts = cli.PlayOpts(
        auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, advance, _q = cli._play_video(
        cfg, "movie", "tt1", "M", opts, auto=True, next_label=None, on_save=None
    )
    assert order == ["fetch-start", "device-resolved"]
    assert (notice, advance) == (None, False)


def test_play_video_cast_fetch_network_error_propagates(monkeypatch):
    """A NetworkError from api.streams must propagate out of _play_video unchanged
    (caught by main()'s top-level api.NetworkError handler), even when the fetch runs
    in the overlapped background thread on the cast path."""
    cfg = Config(torrentio_base="tb", hwdec="")

    def boom(*a, **k):
        raise cli.api.NetworkError("rete giù")

    monkeypatch.setattr(cli.api, "streams", boom)
    monkeypatch.setattr(cli, "_resolve_cast_device", lambda *a, **k: "192.168.1.10")
    opts = cli.PlayOpts(
        auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    with pytest.raises(cli.api.NetworkError, match="rete giù"):
        cli._play_video(cfg, "movie", "tt1", "M", opts, auto=True, next_label=None, on_save=None)


def test_play_video_no_streams_returns_notice(monkeypatch):
    """No streams → return a user-facing notice (surfaced as the menu header)."""
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [])
    monkeypatch.setattr(cli.api, "meta", lambda *a, **k: {})  # released unknown → generic
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, advance, _q = cli._play_video(
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
    monkeypatch.setattr(cli, "_play_video", lambda *a, **k: ("notice", False, 0))
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
        return (None, False, 0)

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
        return ("n", True, 0)

    monkeypatch.setattr(cli, "_play_video", fake)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=True
    )
    play_video = cli._series_player(CFG)
    out = play_video(
        "tt9:1:1", "Show 1x01", opts, auto=False, next_label="nxt", on_save=lambda p, d: None
    )
    assert out == ("n", True, 0)
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
        "Tab: avvia al volo  ·  Alt-C: casta sul TV  ·  Alt-W: watchlist  ·  Ctrl-/: anteprima",
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
    monkeypatch.setattr(cli, "ask_query", lambda *a, **k: "matrix")
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

    def fake(items, prompt, *, header=None, expect=("tab",), preview=None):
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
    actions = [v for v in seen["values"] if isinstance(v, tuple)]
    assert not any(v[0] == cli._BROWSE for v in actions)  # catalogs live in the sections
    labels = " ".join(seen["labels"])
    assert "Film" in labels and "Serie TV" in labels and "Popolari" not in labels
    assert any("sistema" in lab for lab in seen["labels"])  # visual group separator


def test_run_section_series_filters_recent_and_types_actions(monkeypatch):
    """A section menu shows the type-filtered continue-watching and routes typed
    search/browse; its prompt names the section."""
    recent_typs = []
    entry = HistoryEntry(video_id="tt9:1:1", type="series", title="Show", ts=1.0)

    def fake_recent(cfg, limit=30, typ=None):
        recent_typs.append(typ)
        return [entry]

    monkeypatch.setattr(cli.state, "recent", fake_recent)
    actions = [("", (cli._BROWSE, "top")), ("", (cli._SEARCH, "")), None]
    it = iter(actions)
    prompts = []

    def fake_fzf(items, prompt, *, header=None, expect=("tab",), preview=None):
        prompts.append(prompt)
        assert items[0][1] is entry  # the filtered continue-watching row leads the menu
        return next(it)

    monkeypatch.setattr(cli, "fzf_key", fake_fzf)
    monkeypatch.setattr(cli, "ask_query", lambda *a, **k: "fargo")
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
    assert called["browse"] == ("top", "series")
    assert called["search"] == ("fargo", "series")


def test_run_browse_typed_uses_catalog(monkeypatch):
    """With a type, run_browse goes through api.catalog (single-type); without, api.browse."""
    seen = {}

    def catalog(c, typ, cat, *, genre=None, skip=0):
        seen["catalog"] = (typ, cat, genre, skip)
        return []

    def browse(c, cat, *, genre=None, skip=0):
        seen["browse"] = (cat, genre, skip)
        return []

    monkeypatch.setattr(cli.api, "catalog", catalog)
    monkeypatch.setattr(cli.api, "browse", browse)
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli.run_browse(CFG, "top", opts, typ="series") == 1  # empty catalog
    assert seen == {"catalog": ("series", "top", None, 0)}  # api.browse untouched
    seen.clear()
    assert cli.run_browse(CFG, "top", opts) == 1
    assert seen == {"browse": ("top", None, 0)}  # default stays mixed


def test_run_browse_genre_and_pagination(monkeypatch):
    """Genre is forwarded; a full page exposes «altri…» and advances skip."""
    from nstream.api import CATALOG_PAGE

    pages: list[tuple] = []

    def catalog(c, typ, cat, *, genre=None, skip=0):
        pages.append((typ, cat, genre, skip))
        if skip == 0:
            return [{"id": f"tt{i}", "type": typ, "name": f"T{i}"} for i in range(CATALOG_PAGE)]
        return [{"id": "ttX", "type": typ, "name": "Next"}]

    monkeypatch.setattr(cli.api, "catalog", catalog)
    # Page 0: pick «altri…»; page 1: ESC
    more_sent = None
    calls = []

    def fake_fzf(items, prompt, *, header=None, expect=("tab",), preview=None):
        calls.append(([(lab, type(v).__name__) for lab, v in items], header))
        nonlocal more_sent
        if more_sent is None:
            more_sent = items[-1][1]  # the «altri…» sentinel
            assert "altri" in items[-1][0]
            return ("", more_sent)
        return None

    monkeypatch.setattr(cli, "fzf_key", fake_fzf)
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli.run_browse(CFG, "top", opts, typ="movie", genre="Action") == 0
    assert pages == [("movie", "top", "Action", 0), ("movie", "top", "Action", CATALOG_PAGE)]
    assert "Action" in (calls[0][1] or "")


def test_run_genre_picks_then_browses(monkeypatch):
    monkeypatch.setattr(cli, "fzf", lambda items, prompt, **k: "Comedy")
    seen = {}

    def browse(c, cat, o, typ=None, *, genre=None):
        seen["call"] = (cat, typ, genre)
        return 0

    monkeypatch.setattr(cli, "run_browse", browse)
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli.run_genre(CFG, opts, "series") == 0
    assert seen["call"] == ("top", "series", "Comedy")


def test_run_section_offers_genres(monkeypatch):
    seen = {}

    def fake(items, prompt, *, header=None, expect=("tab",), preview=None):
        seen["actions"] = [v for _, v in items if isinstance(v, tuple)]
        return None

    monkeypatch.setattr(cli, "fzf_key", fake)
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli.run_section(CFG, "movie", opts) == 0
    assert (cli._GENRE, "") in seen["actions"]


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
    from nstream import subs

    monkeypatch.setattr(subs.tracks, "probe_tracks", lambda *a, **k: Tracks())
    assert cli.choose_tracks(CFG, "http://u", "movie", "id", "/tmp") == (None, None, ())


def test_choose_tracks_pick_audio_then_play(monkeypatch):
    from nstream import subs

    monkeypatch.setattr(subs.tracks, "probe_tracks", lambda *a, **k: _TR)
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

    monkeypatch.setattr(subs, "fzf", fzf)
    assert cli.choose_tracks(CFG, "http://u", "movie", "id", "/tmp") == (2, None, ())


def test_choose_tracks_esc_returns_none(monkeypatch):
    from nstream import subs

    monkeypatch.setattr(subs.tracks, "probe_tracks", lambda *a, **k: _TR)
    it = iter([None])
    monkeypatch.setattr(subs, "fzf", lambda *a, **k: next(it))
    assert cli.choose_tracks(CFG, "http://u", "movie", "id", "/tmp") is None


def test_choose_tracks_subs_none(monkeypatch):
    from nstream import subs

    monkeypatch.setattr(subs.tracks, "probe_tracks", lambda *a, **k: _TR)

    def fzf(items, prompt, *, header=None):
        if prompt == "riproduzione> " and not hasattr(fzf, "seen"):
            fzf.seen = True
            return items[2][1]  # 💬 Sottotitoli
        if prompt == "sottotitoli> ":
            return items[0][1]  # "nessuno" → "no"
        return items[0][1]  # ▶ Avvia

    monkeypatch.setattr(subs, "fzf", fzf)
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
    notice, _, _q = cli._play_video(
        cfg, "movie", "tt1", "M", opts, auto=True, next_label=None, on_save=None
    )
    assert notice is None


def test_play_video_interactive_calls_track_menu(monkeypatch):
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(
        cli.stream_select,
        "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="": (
            cli.stream_select.VettedStream({"url": "http://u", "name": "S"}, auto, None)
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
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="": (
            cli.stream_select.VettedStream({"url": "http://u", "name": "S"}, auto, None)
        ),
    )
    monkeypatch.setattr(cli, "_resolve_device", lambda c, **k: "TV")

    def boom(*a, **k):
        raise AssertionError("choose_tracks must not run in cast mode")

    monkeypatch.setattr(cli, "choose_tracks", boom)
    monkeypatch.setattr(cast_flow.subs, "auto_subs", lambda *a, **k: subs.SubsPick())
    seen = {}
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(device=k.get("device")) or (0.0, 0.0, False, False),
    )  # fmt: skip
    opts = cli.PlayOpts(
        auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, _, _q = cli._play_video(
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
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="": (
            cli.stream_select.VettedStream({"url": "http://u", "name": "S"}, auto, None)
        ),
    )

    def boom(_cfg, **k):
        raise cli.CastUnavailable("nessun Chromecast in rete")

    monkeypatch.setattr(cli, "_resolve_device", boom)
    monkeypatch.setattr(cli, "choose_tracks", lambda *a, **k: (None, None, ()))

    def no_cast(*a, **k):
        raise AssertionError("cast must not run when no device")

    monkeypatch.setattr(cli, "cast", no_cast)  # _move_to_cast seam
    monkeypatch.setattr(cast_flow.caster, "cast", no_cast)  # cast_flow seam
    seen = {}
    monkeypatch.setattr(cli, "play", lambda *a, **k: seen.update(local=True) or (0.0, 0.0, ""))
    opts = cli.PlayOpts(
        auto=False, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, advance, _q = cli._play_video(
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
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="": (
            cli.stream_select.VettedStream({"url": "http://u", "name": "S"}, auto, None)
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
            seen.update(start=k.get("start"), device=k.get("device")) or (55.0, 100.0, False, False)
        ),
    )
    opts = cli.PlayOpts(
        auto=False, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    notice, advance, _q = cli._play_video(
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


# --- --json dispatch seam (the headless subsystem lives in headless.py) ----


def test_dispatch_json_skips_clear(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_clear", lambda: seen.update(cleared=True))
    monkeypatch.setattr(cli.headless, "run", lambda cfg, args, opts: 0)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    assert cli._dispatch(CFG, argparse.Namespace(json=True), opts) == 0
    assert "cleared" not in seen


def test_main_movies_series_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["nstream", "--movies", "--series", "fargo"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 2


# --- cast decision tree (_play_on_cast → cast_flow.run_cast) ------------------------
#
# These exercise the interactive wrapper end-to-end through the shared decision tree.
# The tree now lives in cast_flow, so the seams are module attributes reachable from it:
# cast_flow.remux / cast_flow.mirror / cast_flow.cast_vet plus
# cast_flow.caster ("cast") and cast_flow.subs ("auto_subs"). Direct helper tests live in
# tests/test_cast_flow.py; here we pin the cli wiring.

_CAST_STREAM = {
    "name": "[RD+] Torrentio\n1080p",
    "title": "Dune.2024.1080p.WEB-DL.HEVC.ITA-GRP\n👤 9 💾 8 GB",
    "url": "http://u/dune.mkv",
}


def _cast_opts(**kw):
    base = dict(auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False)
    base.update(kw)
    return cli.PlayOpts(**base)


def _plan(mode, stream, audio_index=0, real_lang="ita", verified=True):
    return cast_flow.cast_vet.CastAudioPlan(mode, stream, audio_index, real_lang, verified=verified)


def _boom(msg):
    def fail(*a, **k):
        raise AssertionError(msg)

    return fail


def _wire_cast_tree(monkeypatch, plan, *, langs=("ita",)):
    """Hermetic _play_on_cast: always stub vet_cast_audio (the real one ffprobes the url)
    plus the in-cast switch helpers and auto_subs. Returns the spy dict."""
    seen = {"subs": []}
    monkeypatch.setattr(cast_flow.cast_vet, "vet_cast_audio", lambda *a, **k: plan)
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_video",
        lambda cfg, results, chosen, exact_resolution=0: (chosen, ""),
    )
    # Container vetting (ADR 0022): treat the container as castable so these tests keep the
    # direct/mirror paths they assert (the mkv-url stream would otherwise route to a rewrap).
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_container",
        lambda cfg, results, chosen, target, exact_resolution=0: (chosen, False),
    )
    monkeypatch.setattr(cast_flow.cast_vet, "cast_container", lambda cfg, stream: "mp4")
    monkeypatch.setattr(
        cast_flow.cast_vet, "cast_languages", lambda cfg, results, exact_resolution=0: langs
    )
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "cast_resolver",
        lambda cfg, results, exact_resolution=0: lambda lang: "http://u2",
    )
    monkeypatch.setattr(
        cast_flow.subs, "auto_subs",
        lambda cfg, typ, vid, wd, opts, safety_sub_lang=None, **kw: (
            seen["subs"].append(safety_sub_lang) or subs.SubsPick()
        ),
    )  # fmt: skip
    return seen


def _call_cast(opts, stream, *, start=None, results=None):
    return cli._play_on_cast(
        CFG, results if results is not None else [stream], stream, "/tmp", "192.168.1.5",
        typ="movie", video_id="tt1", title="Dune", opts=opts, start=start, next_label=None,
    )  # fmt: skip


def test_play_on_cast_remux_success_uses_cast_file(monkeypatch):
    """Tier-2 plan + remux OK → cast_file(follow=True) with the start threaded; the
    direct cast must never run."""
    stream = dict(_CAST_STREAM)
    seen = _wire_cast_tree(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(
        cast_flow.remux, "remux_for_cast",
        lambda url, cfg, *, audio_index, size_gb=0.0: (
            seen.update(remux_url=url, idx=audio_index) or "/tmp/out.mp4"
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.remux, "cast_file",
        lambda cfg, title, path, **k: (
            seen.update(path=path, follow=k.get("follow"), start=k.get("start"))
            or (0.0, 0.0, False, False)
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.caster, "cast", _boom("direct cast must not run when the remux succeeds")
    )
    _call_cast(_cast_opts(), stream, start=42.0)
    assert seen["remux_url"] == stream["url"] and seen["idx"] == 1  # plan's track is mapped
    assert seen["path"] == "/tmp/out.mp4"
    assert seen["follow"] is True and seen["start"] == 42.0


def test_play_on_cast_remux_failure_degrades_to_direct(monkeypatch, capsys):
    """Interactive twin of the headless remux-failure test: stderr warns and the direct
    cast still carries the ORIGINAL stream url."""
    stream = dict(_CAST_STREAM)
    seen = _wire_cast_tree(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", lambda *a, **k: None)  # ffmpeg failed
    monkeypatch.setattr(
        cast_flow.remux, "cast_file", _boom("cast_file must not run without a remux")
    )
    monkeypatch.setattr(
        cast_flow.caster,
        "cast",
        lambda *a, **k: seen.update(cast_url=a[2]) or (0.0, 0.0, False, False),
    )
    _call_cast(_cast_opts(), stream)
    assert "remux non riuscito" in capsys.readouterr().err
    assert seen["cast_url"] == stream["url"]


def test_play_on_cast_absent_safety_subs_then_direct(monkeypatch, capsys):
    """No dub carries the primary language → auto_subs gets the safety language ONCE,
    stderr explains, then the pick is cast directly anyway."""
    stream = dict(_CAST_STREAM)
    seen = _wire_cast_tree(monkeypatch, _plan("absent", stream, real_lang="eng"))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("no remux for an absent language"))
    monkeypatch.setattr(
        cast_flow.caster,
        "cast",
        lambda *a, **k: seen.update(cast_url=a[2]) or (0.0, 0.0, False, False),
    )
    _call_cast(_cast_opts(), stream)
    err = capsys.readouterr().err
    assert seen["subs"] == [CFG.primary]  # interactive copy: ONE auto_subs call, safety lang set
    # The stubbed auto_subs delivers nothing, so only the audio fact may be stated (the
    # cast-flow twin of this assertion carries the same rationale).
    assert "non disponibile" in err
    assert f"sottotitoli {CFG.primary} attivati" not in err
    assert seen["cast_url"] == stream["url"]


def test_play_on_cast_mirror_gates_on_remux_audio(monkeypatch):
    """--mirror + undecodable audio (plan remux) → cast_via_mirror; remux must not run."""
    stream = dict(_CAST_STREAM)
    seen = _wire_cast_tree(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror",
        lambda cfg, title, url, **k: (
            seen.update(url=url, device=k.get("device"), start=k.get("start"))
            or (0.0, 0.0, False)
        ),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("mirror must preempt the remux"))
    monkeypatch.setattr(
        cast_flow.caster, "cast", _boom("direct cast must not run on the mirror path")
    )
    _call_cast(_cast_opts(mirror=True), stream, start=7.0)
    assert seen["url"] == stream["url"]
    assert seen["device"] == "192.168.1.5" and seen["start"] == 7.0


def test_play_on_cast_explicit_mirror_forces_mirror(monkeypatch):
    """--mirror with DMR-decodable audio now forces the mirror (ADR 0023)."""
    stream = dict(_CAST_STREAM)
    seen = _wire_cast_tree(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror",
        lambda *a, **k: seen.update(mirror=True) or (0.0, 0.0, False),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("direct cast must not run when forced"))
    _call_cast(_cast_opts(mirror=True), stream)
    assert seen.get("mirror") is True


def test_play_on_cast_direct_in_cast_switch_wiring(monkeypatch):
    """Direct cast: the in-cast audio switch (langs + resolve_lang) is wired only when
    several dubs exist; with a single language both stay empty."""
    stream = dict(_CAST_STREAM)
    seen = _wire_cast_tree(monkeypatch, _plan("direct", stream), langs=("ita", "eng"))
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: (
            seen.update(langs=k.get("langs"), resolver=k.get("resolve_lang")) or (0.0, 0.0, False, False)
        ),
    )  # fmt: skip
    _call_cast(_cast_opts(), stream)
    assert seen["langs"] == ("ita", "eng") and callable(seen["resolver"])
    monkeypatch.setattr(
        cast_flow.cast_vet, "cast_languages", lambda cfg, results, exact_resolution=0: ("ita",)
    )
    _call_cast(_cast_opts(), stream)
    assert seen["langs"] == () and seen["resolver"] is None


# --- continue-watching (interactive -c) -------------------------------------


def _VETTED(s):
    return cli.stream_select.VettedStream(stream=s, auto=True, safety_sub_lang=None)


def test_run_continue_empty_history(monkeypatch, capsys):
    monkeypatch.setattr(cli.state, "recent", lambda cfg, limit=30, typ=None: [])
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=True, autoplay=False
    )
    assert cli.run_continue(CFG, opts) == 0
    assert "cronologia vuota" in capsys.readouterr().err


def test_run_continue_plays_picked_entry(monkeypatch):
    entry = HistoryEntry(video_id="tt3", type="movie", title="Dune", ts=1.0)
    monkeypatch.setattr(cli.state, "recent", lambda cfg, limit=30, typ=None: [entry])
    fake_fzf, _ = _fzf_script([("", entry), None])  # pick the entry, then ESC out
    monkeypatch.setattr(cli, "fzf_key", fake_fzf)
    seen = {}
    monkeypatch.setattr(cli, "play_history", lambda cfg, e, o: seen.update(entry=e) or None)
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=True, autoplay=False
    )
    assert cli.run_continue(CFG, opts) == 0
    assert seen["entry"] is entry


def test_play_history_on_save_roundtrip(monkeypatch, tmp_path):
    """After a stubbed play, the history holds the updated entry with the original
    video_id/type/title — the on_save closure built by play_history round-trips."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = Config(torrentio_base="tb", hwdec="")
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(
        cli.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="": _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(cli, "auto_subs", lambda *a, **k: subs.SubsPick())
    monkeypatch.setattr(cli, "play", lambda *a, **k: (42.0, 100.0, ""))
    entry = HistoryEntry(video_id="tt3", type="movie", title="Dune")
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=True, autoplay=False
    )
    assert cli.play_history(cfg, entry, opts) is None
    saved = cli.state.load_history(cfg)["tt3"]
    assert saved["video_id"] == "tt3" and saved["type"] == "movie" and saved["title"] == "Dune"
    assert saved["position"] == 42.0 and saved["duration"] == 100.0


def test_play_history_resume_start_threaded(monkeypatch, tmp_path):
    """A stored position reaches the stubbed play() as --start via _resume_position."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    cfg = Config(torrentio_base="tb", hwdec="")
    cli.state.save_entry(
        cfg,
        {"video_id": "tt3", "title": "Dune", "type": "movie",
         "position": 500.0, "duration": 10000.0, "ts": 1.0},
    )  # fmt: skip
    monkeypatch.setattr(cli.api, "streams", lambda *a, **k: [{"url": "http://u", "name": "S"}])
    monkeypatch.setattr(
        cli.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="": _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(cli, "auto_subs", lambda *a, **k: subs.SubsPick())
    seen = {}
    monkeypatch.setattr(
        cli, "play", lambda *a, **k: seen.update(start=k.get("start")) or (600.0, 10000.0, "")
    )
    entry = HistoryEntry(video_id="tt3", type="movie", title="Dune")
    opts = cli.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None, history=True, autoplay=False
    )
    cli.play_history(cfg, entry, opts)
    assert seen["start"] == 500.0


# --- main() argparse wiring & _entry ---------------------------------------


def _run_main(monkeypatch, argv, cfg, dispatch=None):
    """Drive main() through real argparse with config/theme/dispatch stubbed; returns
    (rc, captured) where captured holds the (cfg, args, opts) _dispatch received."""
    captured = {}

    def fake_dispatch(cfg, args, opts):
        captured.update(cfg=cfg, args=args, opts=opts)
        return 0

    monkeypatch.setattr(cli.sys, "argv", ["nstream", *argv])
    monkeypatch.setattr(cli.log, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda **k: cfg)
    monkeypatch.setattr(cli, "_init_theme", lambda c: None)
    monkeypatch.setattr(cli, "_dispatch", dispatch or fake_dispatch)
    return cli.main(), captured


def test_main_bare_query_routed_to_dispatch(monkeypatch):
    cfg = Config(torrentio_base="tb", auto_play=False)
    rc, seen = _run_main(monkeypatch, ["the", "matrix"], cfg)
    assert rc == 0
    assert seen["cfg"] is cfg
    assert seen["args"].query == ["the", "matrix"]
    opts = seen["opts"]
    assert opts.auto is False and opts.cast is False and opts.mirror is None
    assert opts.history is True and opts.autoplay is True  # cfg defaults pass through


def test_main_json_cast_flags_reach_dispatch(monkeypatch):
    cfg = Config(torrentio_base="tb", auto_play=False)
    rc, seen = _run_main(
        monkeypatch,
        ["--json", "--cast", "--device", "Salotto", "--audio-lang", "ita",
         "--season", "2", "--episode", "5", "dune"],
        cfg,
    )  # fmt: skip
    assert rc == 0
    args, opts = seen["args"], seen["opts"]
    assert args.json is True and args.device == "Salotto"
    assert args.season == 2 and args.episode == 5
    assert opts.auto is True  # --json is headless: always auto-pick
    assert opts.cast is True
    assert opts.audio_lang == "ita"


def test_main_local_overrides_prefer_cast_and_mirror(monkeypatch):
    cfg = Config(torrentio_base="tb", prefer_cast=True, cast_mode="mirror")
    rc, seen = _run_main(monkeypatch, ["--local", "dune"], cfg)
    assert rc == 0
    opts = seen["opts"]
    assert opts.cast is False and opts.mirror is None  # --local beats cfg cast prefs


def test_main_mirror_implies_cast_routing(monkeypatch):
    cfg = Config(torrentio_base="tb")
    rc, seen = _run_main(monkeypatch, ["--mirror", "dune"], cfg)
    assert rc == 0
    opts = seen["opts"]
    assert opts.mirror is True and opts.cast is True


def test_main_no_history_no_autoplay_play_flags(monkeypatch):
    cfg = Config(torrentio_base="tb", auto_play=False)
    rc, seen = _run_main(monkeypatch, ["--play", "--no-history", "--no-autoplay", "dune"], cfg)
    assert rc == 0
    opts = seen["opts"]
    assert opts.auto is True  # --play forces auto even with cfg.auto_play off
    assert opts.history is False and opts.autoplay is False


def test_main_settings_short_circuits_dispatch(monkeypatch):
    cfg = Config(torrentio_base="tb")
    ran = []
    monkeypatch.setattr(cli.settings, "run_settings", lambda c: ran.append(c))
    rc, seen = _run_main(
        monkeypatch, ["--settings"], cfg,
        dispatch=lambda *a, **k: pytest.fail("_dispatch must not run with --settings"),
    )  # fmt: skip
    assert rc == 0
    assert ran == [cfg]


def test_main_explain_without_query_is_usage_error(monkeypatch, capsys):
    """Full wiring: `nstream --explain` (no query) goes through the REAL _dispatch and
    exits 2 with the usage message (no network, no fzf)."""
    cfg = Config(torrentio_base="tb")
    monkeypatch.setattr(cli.sys, "argv", ["nstream", "--explain"])
    monkeypatch.setattr(cli.log, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ensure_config", lambda **k: cfg)
    monkeypatch.setattr(cli, "_init_theme", lambda c: None)
    assert cli.main() == 2
    assert "--explain richiede un titolo" in capsys.readouterr().err


def test_main_network_error_from_dispatch_returns_1(monkeypatch, capsys):
    cfg = Config(torrentio_base="tb")

    def boom(*a, **k):
        raise cli.api.NetworkError("addon irraggiungibile")

    rc, _ = _run_main(monkeypatch, ["dune"], cfg, dispatch=boom)
    assert rc == 1
    assert "addon irraggiungibile" in capsys.readouterr().err


def test_main_config_error_returns_2(monkeypatch, capsys):
    def bad_config(**kwargs):
        raise cli.ConfigError("config rotta")

    monkeypatch.setattr(cli.sys, "argv", ["nstream", "dune"])
    monkeypatch.setattr(cli.log, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ensure_config", bad_config)
    monkeypatch.setattr(
        cli, "_dispatch", lambda *a, **k: pytest.fail("_dispatch must not run without config")
    )
    assert cli.main() == 2
    assert "config rotta" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ("ok", 7),  # main's return code is passed to sys.exit verbatim
        ("interrupt", 130),  # Ctrl-C / EOF → conventional 130
        ("crash", 1),  # unhandled exception → logged, friendly stderr, exit 1
    ],
)
def test_entry_exit_codes(monkeypatch, capsys, outcome, code):
    def fake_main():
        if outcome == "interrupt":
            raise KeyboardInterrupt
        if outcome == "crash":
            raise RuntimeError("boom")
        return 7

    monkeypatch.setattr(cli.log, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli, "main", fake_main)
    with pytest.raises(SystemExit) as e:
        cli._entry()
    assert e.value.code == code
    if outcome == "crash":
        assert "errore inatteso" in capsys.readouterr().err


# --- M2: the --json contract on bootstrap/crash paths ------------------------


def test_main_json_config_error_emits_json_object(monkeypatch, capsys):
    def bad_config(**kwargs):
        raise cli.ConfigError("config rotta")

    monkeypatch.setattr(cli.sys, "argv", ["nstream", "--json", "dune"])
    monkeypatch.setattr(cli.log, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ensure_config", bad_config)
    assert cli.main() == 2
    cap = capsys.readouterr()
    out = json.loads(cap.out)  # an agent parsing stdout still gets its one JSON object
    assert out == {"ok": False, "error": "config", "message": "config rotta"}
    assert "config rotta" in cap.err


def test_ensure_config_headless_never_onboards(monkeypatch, tmp_path):
    """--json is no-fzf by contract: a missing config must raise (→ JSON error),
    never open the interactive onboarding wizard under an agent."""

    def no_config():
        raise cli.ConfigError("manca")

    monkeypatch.setattr(cli, "load", no_config)
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "assente.json")
    monkeypatch.setattr(
        cli.settings, "onboard", lambda: pytest.fail("onboard must not run headless")
    )
    with pytest.raises(cli.ConfigError, match="--settings"):
        cli._ensure_config(headless_mode=True)
