"""Command-line entry point: search/browse → pick (fzf) → play (mpv)."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import cast as typecast

from . import (
    __version__,
    api,
    cast_flow,
    caster,
    debrid,
    explain,
    headless,
    log,
    preview,
    series,
    settings,
    state,
    stream_select,
    tracks,
    ui,
)
from .api import CAT_MAP
from .caster import CastUnavailable, cast
from .caster import resolve_device as _resolve_device
from .config import (
    Config,
    ConfigError,
    HistoryEntry,
    Meta,
    PlayOpts,
    Stream,
    config_path,
    load,
)
from .labels import (
    audio_summary,
    display_title,
    episode_label,
    history_label,
    meta_label,
    sub_summary,
    track_label,
)
from .picker import fzf, fzf_key
from .player import play
from .subs import auto_subs, pick_subtitles

_log = log.get_logger("cli")


def _clear() -> None:
    """Wipe the terminal (screen + scrollback) so menus and mpv output never pile up.
    No-op when stdout isn't a TTY (tests, pipes) so non-interactive runs stay clean."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[H\x1b[2J\x1b[3J")
        sys.stdout.flush()


def _init_theme(cfg: Config) -> None:
    # Pin the active caps once; label builders (labels.py) and the menus below read them
    # on demand via ui.active_caps(). Sensible defaults keep direct calls (tests) working.
    ui.set_active_caps(ui.detect_caps(cfg))


# --- pre-play audio/subtitle track menu ----------------------------------


def choose_tracks(
    cfg: Config, url: str, typ: str, video_id: str, work_dir: str
) -> tuple[int | None, str | int | None, tuple[str, ...]] | None:
    """Pre-play menu to pick the audio/subtitle track from those actually in the file
    (probed with ffprobe). Returns (audio_id, sub_id, sub_paths), or None if the user
    backs out (ESC). With ffprobe unavailable, skips silently to mpv's defaults."""
    tr = tracks.probe_tracks(url)
    if tr.empty():
        print("nstream: tracce non sondabili (ffprobe assente?), uso i default", file=sys.stderr)
        return (None, None, ())

    aid: int | None = None
    sid: int | str | None = None
    sub_paths: tuple[str, ...] = ()
    # Sentinels: fzf returns None for ESC, so "automatic" can't be a None *value*.
    _PLAY, _AUDIO, _SUBS, _AUTO, _OPENSUBS = (object() for _ in range(5))
    while True:
        items: list[tuple[str, object]] = [
            (f"{ui.g().play}  Avvia", _PLAY),
            (f"{ui.g().audio} Audio: {audio_summary(aid, tr)}", _AUDIO),
            (f"{ui.g().subs} Sottotitoli: {sub_summary(sid, sub_paths, tr)}", _SUBS),
        ]
        chosen = fzf(items, "riproduzione> ")
        if chosen is None:
            return None
        if chosen is _PLAY:
            return (aid, sid, sub_paths)
        if chosen is _AUDIO:
            opts: list[tuple[str, object]] = [("automatico (lingua preferita)", _AUTO)]
            opts += [(track_label(a), a.id) for a in tr.audio]
            pick = fzf(opts, "audio> ")
            if pick is _AUTO:
                aid = None
            elif pick is not None:
                aid = typecast(int, pick)
        else:  # _SUBS
            sopts: list[tuple[str, object]] = [("nessuno", "no")]
            sopts += [(track_label(s), s.id) for s in tr.subs]
            sopts.append(("OpenSubtitles… (esterni)", _OPENSUBS))
            pick = fzf(sopts, "sottotitoli> ")
            if pick is None:
                continue
            if pick is _OPENSUBS:
                got = pick_subtitles(cfg, typ, video_id, work_dir, mode="menu")
                if got:
                    sub_paths, sid = got, None
            else:
                sid, sub_paths = typecast("str | int", pick), ()


# --- flow ----------------------------------------------------------------


def _play_video(
    cfg: Config,
    typ: str,
    video_id: str,
    title: str,
    opts: PlayOpts,
    *,
    auto: bool,
    next_label: str | None,
    on_save: Callable[[float, float], None] | None,
    reselect_on_wrong_audio: bool = True,
    cast_meta: caster.CastMeta | None = None,
) -> tuple[str | None, bool]:
    """Resolve streams for one video, play it, persist progress. Returns
    (notice, advance): `notice` is a user-facing message to surface (no streams /
    not released yet) or None on success or a cancelled stream menu; `advance` is
    True when the next-episode overlay asked to continue.

    `auto` overrides `opts.auto` for this single video: the binge loop forces it
    True from the second episode on, so use `auto` (not `opts.auto`) here."""
    # Resolve the cast device BEFORE stream selection: ranking is profile-dependent
    # (Chromecast receiver caps vs the local GPU), so when no device is reachable we
    # must select for local mpv — not play a TV-filtered pick (e.g. AV1 dropped as
    # "no-HW" even though the local GPU decodes it) on the laptop.
    # Resolving streams (Torrentio + RD) can take a moment; without a menu to mask
    # the wait, say what's happening so the TUI doesn't look frozen.
    print(f"{ui.g().play} {title} — cerco la sorgente migliore…", file=sys.stderr)
    if opts.cast:
        # Overlap the stream fetch (profile-independent) with the catt scan (up to
        # ~2×10s): only ranking/selection depends on the device, and that runs after
        # the join. Daemon thread (same pattern as player/serve), NOT an executor:
        # non-daemon workers would outlive a confirm prompt aborted with Ctrl-C.
        fetched: list[list[Stream]] = []
        fetch_err: list[BaseException] = []

        def _fetch_streams() -> None:
            try:
                fetched.append(api.streams(cfg, typ, video_id))
            except BaseException as e:  # noqa: BLE001 — re-raised in the main thread
                fetch_err.append(e)

        th = threading.Thread(target=_fetch_streams, name="streams-fetch", daemon=True)
        th.start()
        device = _resolve_cast_device(cfg, opts)
        th.join()
        if fetch_err:
            raise fetch_err[0]  # preserve api.streams' propagation (e.g. NetworkError)
        results = fetched[0] if fetched else []
    else:
        device = None
        results = api.streams(cfg, typ, video_id)
    if opts.cast and device is None:
        opts = replace(opts, cast=False)  # degrade: select and play with the local profile
    if not results:
        notice = stream_select.no_streams_message(cfg, typ, video_id, title)
        print(f"nstream: {notice}", file=sys.stderr)
        return (notice, False)
    vetted = stream_select.prepare_stream(
        cfg, results, opts, auto=auto, reselect_on_wrong_audio=reselect_on_wrong_audio
    )
    if vetted is None:
        return (None, False)  # no playable stream, or backed out of a (re)selection
    chosen, auto, safety_sub_lang = vetted.stream, vetted.auto, vetted.safety_sub_lang

    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="nstream-", dir=runtime) as work_dir:
        start = state.resume_position(cfg, video_id) if opts.history else None
        name_line = next(iter((chosen.get("name") or "").splitlines()), "")
        if device is not None:
            print(f"{ui.g().play} {title} — {name_line}", file=sys.stderr)
            pos, dur, advance = _play_on_cast(
                cfg, results, chosen, work_dir, device,
                typ=typ, video_id=video_id, title=title, opts=opts,
                start=start, next_label=next_label, safety_sub_lang=safety_sub_lang,
                cast_meta=cast_meta,
            )  # fmt: skip
        else:
            print(f"{ui.g().play} {title} — {name_line}", file=sys.stderr)
            res = _play_on_mpv(
                cfg, chosen, work_dir,
                typ=typ, video_id=video_id, title=title, opts=opts,
                start=start, next_label=next_label, auto=auto, safety_sub_lang=safety_sub_lang,
            )  # fmt: skip
            if res is None:
                return (None, False)  # backed out of the track menu → return to the list
            pos, dur, advance = res
    _clear()  # drop mpv's exit frame/logs before returning to the menu
    # Only persist a resume we can reason about: a real duration is needed for the
    # watched/near-end logic, otherwise the entry would stick forever.
    if opts.history and on_save and pos > 0 and dur > 0:
        on_save(pos, dur)
    return (None, advance)


def _resolve_cast_device(cfg: Config, opts: PlayOpts) -> str | None:
    """Resolve a Chromecast for this play, or None to fall back to local mpv when the
    LAN has no reachable device (e.g. after a network change) or the user declines the
    auto-cast confirmation. An explicit cast action (Alt-C → `cast_choose`) is its own
    confirmation, so only the auto route (`prefer_cast`/`--cast`) asks."""
    try:
        return _resolve_device(cfg, choose=opts.cast_choose, confirm=not opts.cast_choose)
    except CastUnavailable as e:
        print(f"nstream: {e} — riproduco in locale", file=sys.stderr)
        _log.info("cast non attivo (%s) → fallback locale", e)
        return None


def _play_on_cast(
    cfg: Config,
    results: list[Stream],
    chosen: Stream,
    work_dir: str,
    device: str,
    *,
    typ: str,
    video_id: str,
    title: str,
    opts: PlayOpts,
    start: float | None,
    next_label: str | None,
    safety_sub_lang: str | None = None,
    cast_meta: caster.CastMeta | None = None,
) -> tuple[float, float, bool]:
    """Interactive cast: thin wrapper over the shared decision tree (`cast_flow.run_cast`),
    with the interactive knobs on — blocking follow, next-episode label, and the in-cast
    audio switch (re-cast a differently-dubbed release) when several dubs exist."""
    outcome = cast_flow.run_cast(
        cfg, results, chosen,
        device=device, title=title, typ=typ, video_id=video_id, work_dir=work_dir,
        opts=opts, start=start, follow=True, next_label=next_label,
        allow_lang_switch=True, meta=cast_meta, safety_sub_lang=safety_sub_lang,
    )  # fmt: skip
    return (outcome.pos, outcome.dur, outcome.advance)


def _play_on_mpv(
    cfg: Config,
    chosen: Stream,
    work_dir: str,
    *,
    typ: str,
    video_id: str,
    title: str,
    opts: PlayOpts,
    start: float | None,
    next_label: str | None,
    auto: bool,
    safety_sub_lang: str | None = None,
) -> tuple[float, float, bool] | None:
    """Play locally in mpv. Returns (pos, dur, advance), or None if the user backed
    out of the pre-play track menu (so the caller returns to the list)."""
    audio_id: int | None = None
    sub_id: str | int | None = None
    if auto:
        sub_paths = auto_subs(cfg, typ, video_id, work_dir, opts, safety_sub_lang=safety_sub_lang)
    else:
        sel = choose_tracks(cfg, chosen["url"], typ, video_id, work_dir)
        if sel is None:
            return None
        audio_id, sub_id, sub_paths = sel
    cast_ok = shutil.which("catt") is not None  # enable in-player Alt-C → TV
    pos, dur, signal = play(
        cfg, title, chosen["url"],
        start=start, sub_paths=sub_paths, audio_id=audio_id, sub_id=sub_id,
        next_label=next_label, cast_enabled=cast_ok, work_dir=work_dir,
    )  # fmt: skip
    if signal == "cast":  # Alt-C in mpv: move this playback to the TV from `pos`
        return _move_to_cast(cfg, title, chosen, pos, dur)
    return (pos, dur, signal == "next")


def _move_to_cast(
    cfg: Config, title: str, chosen: Stream, pos: float, dur: float
) -> tuple[float, float, bool]:
    """Hand the running mpv position over to a Chromecast (in-player Alt-C). Keeps the
    local pos/dur if no device resolves. Never auto-advances (the user is switching)."""
    try:
        device = _resolve_device(cfg, choose=True)
    except CastUnavailable as e:
        print(f"nstream: {e}", file=sys.stderr)
        return (pos, dur, False)
    pos, dur, _ = cast(cfg, title, chosen["url"], device=device, start=pos)
    return (pos, dur, False)


def _series_player(cfg: Config) -> series.PlayVideo:
    """Adapt `_play_video` to the series-flow callable (`series.PlayVideo`): cfg and
    typ="series" pre-bound, so `series.py` plays episodes without importing cli."""

    def play_video(
        video_id: str,
        title: str,
        opts: PlayOpts,
        *,
        auto: bool,
        next_label: str | None,
        on_save: Callable[[float, float], None],
        reselect_on_wrong_audio: bool = True,
        cast_meta: caster.CastMeta | None = None,
    ) -> tuple[str | None, bool]:
        return _play_video(
            cfg, "series", video_id, title, opts,
            auto=auto, next_label=next_label, on_save=on_save,
            reselect_on_wrong_audio=reselect_on_wrong_audio, cast_meta=cast_meta,
        )  # fmt: skip

    return play_video


def play_meta(cfg: Config, meta: Meta, opts: PlayOpts) -> str | None:
    """Play a title; returns a notice to show above the list, or None.
    Thin type dispatch: series (episode picker + binge) live in `series.py`."""
    typ = meta.get("type", "movie")
    if typ == "series":
        return series.play(
            cfg, meta, opts,
            play_video=_series_player(cfg), pick_hint=_pick_hint, apply_key=_apply_key,
        )  # fmt: skip

    name = meta.get("name", "nstream")
    movie_id = meta["id"]

    def on_save(pos: float, dur: float) -> None:
        state.save_entry(cfg, state.make_entry(movie_id, name, typ, pos, dur))

    notice, _ = _play_video(
        cfg, typ, movie_id, display_title(name, None), opts,
        auto=opts.auto, next_label=None, on_save=on_save,
        cast_meta=caster.CastMeta(poster=meta.get("poster") or ""),
    )  # fmt: skip
    return notice


def play_history(cfg: Config, entry: HistoryEntry, opts: PlayOpts) -> str | None:
    """Resume from a history entry; returns a notice to show, or None.
    Series entries (binge resume + single-episode fallback) dispatch to `series.py`."""
    typ = entry.get("type", "movie")
    if typ == "series":
        return series.resume(cfg, entry, opts, play_video=_series_player(cfg))

    name = entry.get("title", "nstream")
    video_id = entry["video_id"]

    def on_save(pos: float, dur: float) -> None:
        state.save_entry(cfg, state.make_entry(video_id, name, typ, pos, dur))

    notice, _ = _play_video(
        cfg, typ, video_id, display_title(name, None), opts,
        auto=opts.auto, next_label=None, on_save=on_save,
    )  # fmt: skip
    return notice


def _pick_hint(opts: PlayOpts) -> str:
    """Discoverability line for the leaf lists: Tab flips the play mode, Alt-C casts."""
    tab = "Tab: scegli sorgente/tracce" if opts.auto else "Tab: avvia al volo"
    return f"{tab}  ·  Alt-C: casta sul TV"


def _apply_key(opts: PlayOpts, key: str) -> PlayOpts:
    """Map a leaf-list selection key to per-pick options: Alt-C casts this title
    (forcing the device picker); Tab flips auto↔manual; Enter keeps the default."""
    if key == "alt-c":
        return replace(opts, cast=True, cast_choose=True)
    return replace(opts, auto=opts.auto ^ (key == "tab"))


def _meta_preview(m: Meta) -> str | None:
    """The `__preview` token for a title row (poster + metadata pane), or None."""
    vid = m.get("id")
    return f"title {m.get('type', 'movie')} {vid}" if vid else None


def _entry_preview(e: HistoryEntry) -> str | None:
    """Preview token for a continue-watching row: the episode for a series, else the title."""
    if e.get("type") == "series" and e.get("series_id"):
        return f"episode {e['series_id']} {e.get('season', 0)} {e.get('episode', 0)}"
    vid = e.get("video_id")
    return f"title {e.get('type', 'movie')} {vid}" if vid else None


def _pick_meta(items: list[tuple[str, Meta]], cfg: Config, opts: PlayOpts) -> int:
    """Loop the title list: play a pick, then return here. ESC leaves to the caller
    (HOME or the shell). A notice from playback is shown as the fzf header next time.
    Enter plays with the default mode; Tab flips auto↔manual for that pick (series
    defer the choice to the episode picker)."""
    header: str | None = None
    while True:
        chosen = fzf_key(
            items, "titolo> ", header=header or _pick_hint(opts), preview=_meta_preview
        )
        if not chosen:
            return 0
        key, meta = chosen
        # Series defer auto/manual to the episode picker, but Alt-C (cast) still applies.
        sel = replace(opts, cast=True, cast_choose=True) if key == "alt-c" else opts
        if meta.get("type") != "series":
            sel = _apply_key(opts, key)
        header = play_meta(cfg, meta, sel)


def run_search(cfg: Config, query: str, opts: PlayOpts, typ: str | None = None) -> int:
    metas = api.search(cfg, query, typ)
    if not metas:
        print("nstream: nessun risultato", file=sys.stderr)
        return 1
    return _pick_meta([(meta_label(m), m) for m in metas], cfg, opts)


def run_browse(cfg: Config, cat: str, opts: PlayOpts, typ: str | None = None) -> int:
    # Typed → single-type catalog; None → movies + series, fetched concurrently.
    metas = api.catalog(cfg, typ, cat) if typ else api.browse(cfg, cat)
    if not metas:
        print("nstream: catalogo vuoto", file=sys.stderr)
        return 1
    return _pick_meta([(meta_label(m), m) for m in metas], cfg, opts)


def run_explain(cfg: Config, query: str) -> int:
    """`--explain`: search → pick a title (and episode, for series) → print WHY the
    auto-pick won (ranking table for local + cast profiles, plus the audio decision).
    Read-only: never plays or casts."""
    metas = api.search(cfg, query)
    if not metas:
        print("nstream: nessun risultato", file=sys.stderr)
        return 1
    meta = fzf([(meta_label(m), m) for m in metas], "titolo> ", preview=_meta_preview)
    if meta is None:
        return 0
    typ = meta.get("type", "movie")
    video_id = meta["id"]
    title = meta.get("name", "?")
    if typ == "series":
        eps = api.episodes(cfg, video_id)
        if not eps:
            print(f"nstream: nessun episodio per «{title}»", file=sys.stderr)
            return 1
        v = fzf([(episode_label(e), e) for e in eps], "episodio> ")
        if v is None:
            return 0
        video_id = v["id"]
        title = display_title(title, v)
    results = api.streams(cfg, typ, video_id)
    print(f"\n# nstream --explain · {title}\n")
    print(explain.explain_streams(cfg, results, cast=False))
    print()
    print(explain.explain_streams(cfg, results, cast=True))
    print()
    print(explain.explain_audio(cfg, explain.auto_pick(cfg, results, cast=False)))
    return 0


def run_continue(cfg: Config, opts: PlayOpts, typ: str | None = None) -> int:
    """`-c`: resume from history, returning to the list after each play (ESC exits).
    `typ` (--movies/--series) narrows the list to one content type."""
    entries = state.recent(cfg, typ=typ)
    if not entries:
        print("nstream: cronologia vuota", file=sys.stderr)
        return 0
    header: str | None = None
    while True:
        items = [(history_label(e), e) for e in entries]
        chosen = fzf_key(
            items, "continua> ", header=header or _pick_hint(opts), preview=_entry_preview
        )
        if chosen is None:
            return 0
        key, entry = chosen
        header = play_history(cfg, entry, _apply_key(opts, key))
        entries = state.recent(cfg, typ=typ)  # reflect updated positions, then re-show


# Home-menu action kinds (the value half of an fzf item; history entries are dicts).
_SEARCH = "search"
_BROWSE = "browse"
_SECTION = "section"
_SETTINGS = "settings"

# Section type → fzf prompt (home itself uses "nstream> ").
_SECTION_PROMPT = {"movie": "film> ", "series": "serie> "}


def _home_preview(value: object) -> str | None:
    """Action rows (tuples) have no preview; continue-watching entries (dicts) do."""
    return None if isinstance(value, tuple) else _entry_preview(typecast("HistoryEntry", value))


def _ask_query() -> str | None:
    """Prompt for a search query on stdin; None on EOF (leave the menu)."""
    try:
        return input("cerca> ").strip()
    except EOFError:
        return None


def run_home(cfg: Config, opts: PlayOpts) -> int:
    """The TUI home: continue-watching + search + the typed sections (Film / Serie TV)
    + settings, in one menu. Loops until the user backs out (ESC). This is the rich
    entry surface — the desktop/fuzzel launcher only opens it; no UI logic in fuzzel."""
    return _home_menu(cfg, opts, typ=None)


def run_section(cfg: Config, typ: str, opts: PlayOpts) -> int:
    """A type-scoped home section: type-filtered continue-watching, search and the
    three Cinemeta catalogs, all pinned to `typ`. ESC returns to the home menu."""
    return _home_menu(cfg, opts, typ=typ)


def _home_menu(cfg: Config, opts: PlayOpts, *, typ: str | None) -> int:
    """Shared loop behind run_home (typ None: mixed rows + sections + settings) and
    run_section (typ set: rows and catalogs pinned to one type)."""
    notice: str | None = None
    while True:
        recent = state.recent(cfg, typ=typ) if opts.history else []
        g = ui.glyphs(ui.active_caps())
        items: list[tuple[str, object]] = [(history_label(e), e) for e in recent]
        items.append((f"{g.search}  Cerca…", (_SEARCH, "")))
        if typ is None:  # home: the typed sections own the catalogs
            items += [
                (f"{g.movie}  Film", (_SECTION, "movie")),
                (f"{g.series}  Serie TV", (_SECTION, "series")),
                (f"{g.gear}  Impostazioni", (_SETTINGS, "")),
            ]
        else:  # section: the three catalogs, served per-type by api.catalog
            items += [
                (f"{g.fire}  Popolari", (_BROWSE, "popolari")),
                (f"{g.new}  Novità", (_BROWSE, "nuovi")),
                (f"{g.star}  Top IMDb", (_BROWSE, "top")),
            ]

        # The Tab hint only applies to the continue-watching rows.
        header = notice or (_pick_hint(opts) if recent else None)
        prompt = _SECTION_PROMPT.get(typ or "", "nstream> ")
        chosen = fzf_key(items, prompt, header=header, preview=_home_preview)
        notice = None
        if chosen is None:
            return 0
        key, value = chosen
        if not isinstance(value, tuple):  # a continue-watching entry
            notice = play_history(cfg, typecast("HistoryEntry", value), _apply_key(opts, key))
            continue
        kind, value = value
        if kind == _SEARCH:
            query = _ask_query()
            if query is None:
                return 0
            if query:
                run_search(cfg, query, opts, typ)
        elif kind == _SECTION:
            run_section(cfg, typecast(str, value), opts)
        elif kind == _BROWSE:
            run_browse(cfg, CAT_MAP[typecast(str, value)], opts, typ)
        elif kind == _SETTINGS:
            settings.run_settings(cfg)
            cfg = load()  # pick up any change for the next loop


def _dispatch(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    if args.json:
        # Headless: no _clear, no fzf, JSON on stdout (incl. the NetworkError → JSON guard).
        return headless.run(cfg, args, opts)
    typ = headless.typ_filter(args)
    _clear()  # start the interactive session on a clean screen (drop launcher banner)
    if args.cont:
        return run_continue(cfg, opts, typ)
    if args.browse:
        return run_browse(cfg, CAT_MAP[args.browse], opts, typ)
    query = " ".join(args.query)
    if args.explain:
        if not query:
            print("nstream: --explain richiede un titolo da cercare", file=sys.stderr)
            return 2
        return run_explain(cfg, query)
    if query:
        return run_search(cfg, query, opts, typ)
    return run_home(cfg, opts)  # the home has the typed sections; flags don't apply


def _sub_options(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.sub_lang:
        return ("auto", args.sub_lang)
    if args.sub_menu:
        return ("menu", None)
    if args.subs:
        return ("auto", None)
    return (None, None)


def _ensure_config() -> Config:
    """Load config, running first-run onboarding if it's missing."""
    try:
        return load()
    except ConfigError:
        if not config_path().exists():
            settings.onboard()  # prompts for the RD token, writes a minimal config
            return load()
        raise


def main() -> int:
    # Hidden fast path: fzf invokes `nstream __preview …` per focused row. Handle it
    # before argparse (it must stay lightweight and not collide with the query positional).
    if sys.argv[1:2] == ["__preview"]:
        return preview.run_preview(sys.argv[2:])
    # Same for `nstream __layout`: fzf's resize transform, re-derives the preview placement.
    if sys.argv[1:2] == ["__layout"]:
        return preview.run_layout()

    parser = argparse.ArgumentParser(
        prog="nstream",
        description="Native Stremio-like client (Cinemeta + Torrentio + Real-Debrid + mpv).",
    )
    parser.add_argument("query", nargs="*", help="titolo da cercare (altrimenti chiede)")
    parser.add_argument(
        "--play",
        action="store_true",
        help="forza la riproduzione automatica (anche se disattivata)",
    )
    parser.add_argument(
        "--cast", action="store_true", help="manda lo stream a un Chromecast (catt) invece di mpv"
    )
    parser.add_argument(
        "--mirror",
        action="store_true",
        help="cast via mirror nativo (mpv su output headless → sender): parte subito, 1080p SDR",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="forza la riproduzione locale in mpv (anche se il default è cast)",
    )
    parser.add_argument(
        "--subs", action="store_true", help="sottotitoli automatici nella lingua preferita"
    )
    parser.add_argument("--sub-menu", action="store_true", help="scegli i sottotitoli a mano (fzf)")
    parser.add_argument("--sub-lang", metavar="CODE", help="lingua sottotitoli da auto-scegliere")
    parser.add_argument(
        "--browse", nargs="?", const="popolari", choices=list(CAT_MAP),
        help="sfoglia un catalogo Cinemeta invece di cercare (default: popolari)",
    )  # fmt: skip
    typ_group = parser.add_mutually_exclusive_group()
    typ_group.add_argument(
        "--movies", action="store_true", help="solo film (ricerca, catalogo, cronologia)"
    )
    typ_group.add_argument(
        "--series", action="store_true", help="solo serie TV (ricerca, catalogo, cronologia)"
    )
    parser.add_argument(
        "-c", "--continue", dest="cont", action="store_true",
        help="riprendi dalla cronologia (continua a guardare)",
    )  # fmt: skip
    parser.add_argument("--no-history", action="store_true", help="non salvare la cronologia")
    parser.add_argument(
        "--no-autoplay", action="store_true", help="non proporre il prossimo episodio"
    )
    parser.add_argument("--settings", action="store_true", help="apri il menu impostazioni")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="spiega perché uno stream/audio verrebbe scelto (non riproduce)",
    )
    parser.add_argument(
        "--debrid-test",
        metavar="INFOHASH",
        help="diagnostica: prova cache+resolve del provider debrid nativo (aggiunge il torrent)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="modalità headless non-interattiva: niente fzf, un oggetto JSON su stdout",
    )
    parser.add_argument("--year", metavar="YYYY", help="disambigua il titolo per anno (--json)")
    parser.add_argument(
        "--season", type=int, metavar="N", help="stagione serie (--json, default 1)"
    )
    parser.add_argument(
        "--episode", type=int, metavar="M", help="episodio serie (--json, default 1)"
    )
    parser.add_argument(
        "--device", metavar="NAME", help="Chromecast di destinazione (--json, evita il picker)"
    )
    parser.add_argument(
        "--audio-lang", metavar="CODE", help="forza la lingua audio/dub (es. eng, ita) (--json)"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="--json: elenca audio/sottotitoli disponibili per il titolo, non riproduce",
    )
    parser.add_argument(
        "--stop", action="store_true", help="--json: ferma il cast in corso, non riproduce"
    )
    parser.add_argument(
        "--status", action="store_true", help="--json: stato del cast (player_state, titolo…)"
    )
    parser.add_argument(
        "--volume",
        type=int,
        metavar="N",
        help="--json+cast: imposta il volume del Chromecast (0-100)",
    )
    parser.add_argument(
        "--follow",
        dest="follow",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--json+cast: segui fino a fine (resume) o ritorna subito (--no-follow, default)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="log verboso su stderr (oltre al file di log)"
    )
    parser.add_argument("--version", action="version", version=f"nstream {__version__}")
    args = parser.parse_args()

    log.setup_logging(args.debug or bool(os.environ.get("NSTREAM_DEBUG")))
    _log.info("nstream %s avvio (cast=%s)", __version__, args.cast or "")
    _log.debug("args: %r", vars(args))

    try:
        cfg = _ensure_config()
    except ConfigError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return 2

    _init_theme(cfg)

    if args.settings:
        settings.run_settings(cfg)
        return 0

    if args.debrid_test:
        print(debrid.selftest(cfg, args.debrid_test))
        return 0

    sub_mode, sub_lang = _sub_options(args)
    # Mirror is a cast backend: it implies cast routing (device resolution), unless local.
    mirror_mode = (args.mirror or cfg.cast_mode == "mirror") and not args.local
    opts = PlayOpts(
        # --json is headless: always auto-pick (no fzf stream menu).
        auto=cfg.auto_play or args.play or args.json,
        cast=(cfg.prefer_cast or args.cast or mirror_mode) and not args.local,
        sub_mode=sub_mode,
        sub_lang=sub_lang,
        history=cfg.history_enabled and not args.no_history,
        autoplay=cfg.autoplay and not args.no_autoplay,
        audio_lang=args.audio_lang or None,
        mirror=mirror_mode,
    )
    try:
        return _dispatch(cfg, args, opts)
    except api.NetworkError as e:
        _log.warning("network: %s", e)
        print(f"nstream: {e}", file=sys.stderr)
        return 1


def _entry() -> None:
    # Configure logging before anything else so a crash in main() is captured even
    # when nstream runs inside the foot launcher (where the traceback would scroll away).
    log.setup_logging(bool(os.environ.get("NSTREAM_DEBUG")))
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
    except Exception:
        _log.exception("crash non gestito")
        print(f"nstream: errore inatteso — dettagli in {log.log_path()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _entry()
