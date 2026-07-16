"""Command-line entry point: search/browse → pick (fzf) → play (mpv)."""

from __future__ import annotations

import argparse
import json
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
    discovery,
    explain,
    headless,
    log,
    preview,
    series,
    settings,
    state,
    stream_select,
    ui,
)
from . import (
    quality as quality_mod,
)
from . import subs as subs_mod
from .api import CAT_MAP, CATALOG_PAGE, GENRES
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
    display_title,
    episode_label,
    history_label,
    meta_label,
)
from .picker import ask_query, fzf, fzf_key
from .player import play
from .subs import auto_subs, choose_tracks  # re-export: tests + _play_on_mpv

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
) -> tuple[str | None, bool, int]:
    """Resolve streams for one video, play it, persist progress. Returns
    (notice, advance, quality): `notice` is a user-facing message to surface (no
    streams / not released yet) or None on success or a cancelled stream menu;
    `advance` is True when the next-episode overlay asked to continue; `quality`
    is the resolved quality choice (0 = Auto, N = exact res) for series binge sticky.

    `auto` overrides `opts.auto` for this single video: the binge loop forces it
    True from the second episode on, so use `auto` (not `opts.auto`) here."""
    # Resolve the cast device BEFORE stream selection: ranking is profile-dependent
    # (Chromecast receiver caps vs the local GPU), so when no device is reachable we
    # must select for local mpv — not play a TV-filtered pick (e.g. AV1 dropped as
    # "no-HW" even though the local GPU decodes it) on the laptop.
    # Resolving streams (Torrentio + debrid) can take a moment; without a menu to mask
    # the wait, say what's happening so the TUI doesn't look frozen. Title once as
    # context; later phases are short verbs (no re-banner of the full title).
    ui.status(title, kind="play")
    ui.status("cerco sorgente…", kind="search")
    if opts.cast:
        # Overlap the stream fetch (profile-independent) with device resolution (near
        # instant via the verified cache / background scan; a short bounded wait at
        # worst): only ranking/selection depends on the device, and that runs after
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
        ui.status(notice, kind="fail")
        return (notice, False, opts.quality if opts.quality is not None else 0)
    vetted = stream_select.prepare_stream(
        cfg,
        results,
        opts,
        auto=auto,
        reselect_on_wrong_audio=reselect_on_wrong_audio,
        title=title,
    )
    if vetted is None:
        # No playable stream, or backed out of a (re)selection / quality picker.
        return (None, False, opts.quality if opts.quality is not None else 0)
    chosen, auto, safety_sub_lang = vetted.stream, vetted.auto, vetted.safety_sub_lang
    quality_choice = vetted.quality
    # ADR 0021: from here on opts.quality is ALWAYS the resolved int (0=Auto, N=exact) —
    # the cast decision tree threads it through every reselect path (series.py already
    # relies on the same replace() for the binge sticky).
    opts = replace(opts, quality=quality_choice)

    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="nstream-", dir=runtime) as work_dir:
        start = state.resume_position(cfg, video_id) if opts.history else None
        name_line = next(iter((chosen.get("name") or "").splitlines()), "") or "sorgente"
        ui.status(f"scelto {name_line}", kind="play")
        if device is not None:
            pos, dur, advance = _play_on_cast(
                cfg, results, chosen, work_dir, device,
                typ=typ, video_id=video_id, title=title, opts=opts,
                start=start, next_label=next_label, safety_sub_lang=safety_sub_lang,
                cast_meta=cast_meta,
            )  # fmt: skip
        else:
            res = _play_on_mpv(
                cfg, chosen, work_dir,
                typ=typ, video_id=video_id, title=title, opts=opts,
                start=start, next_label=next_label, auto=auto, safety_sub_lang=safety_sub_lang,
            )  # fmt: skip
            if res is None:
                # Backed out of the track menu → return to the list; keep quality sticky.
                return (None, False, quality_choice)
            pos, dur, advance = res
    _clear()  # drop mpv's exit frame/logs before returning to the menu
    # Only persist a resume we can reason about: a real duration is needed for the
    # watched/near-end logic, otherwise the entry would stick forever.
    if opts.history and on_save and pos > 0 and dur > 0:
        on_save(pos, dur)
    return (None, advance, quality_choice)


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
    try:
        outcome = cast_flow.run_cast(
            cfg, results, chosen,
            device=device, title=title, typ=typ, video_id=video_id, work_dir=work_dir,
            opts=opts, start=start, follow=True, next_label=next_label,
            allow_lang_switch=True, meta=cast_meta, safety_sub_lang=safety_sub_lang,
        )  # fmt: skip
    except cast_flow.CastVideoUnsupported as e:
        # Casting anyway would show a black screen (ADR 0017): back out to the list with
        # an honest message instead. Local mpv decodes anything → suggest it.
        print(
            f"nstream: {ui.g().warn} video {e.codec.upper()} non decodificabile dal TV "
            "e nessuna alternativa castabile — riproduci in locale o scegli un'altra release",
            file=sys.stderr,
        )
        return (0.0, 0.0, False)
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
        sub_paths = auto_subs(
            cfg, typ, video_id, work_dir, opts, safety_sub_lang=safety_sub_lang,
            video_url=chosen.get("url"), filename=subs_mod.stream_filename(chosen),
        ).paths  # fmt: skip
    else:
        sel = choose_tracks(cfg, chosen["url"], typ, video_id, work_dir)
        if sel is None:
            return None
        audio_id, sub_id, sub_paths = sel
    # Device discovery still needs catt (scan); castbridge is the preferred *delivery*
    # backend once a device IP is known. Without catt, Alt-C has no way to resolve a target.
    cast_ok = shutil.which("catt") is not None
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
    state.clear_cast_session()  # Alt-C bypasses run_cast, which normally does this
    pos, dur, _, _ = cast(cfg, title, chosen["url"], device=device, start=pos)
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
    ) -> tuple[str | None, bool, int]:
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

    notice, _, _ = _play_video(
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

    notice, _, _ = _play_video(
        cfg, typ, video_id, display_title(name, None), opts,
        auto=opts.auto, next_label=None, on_save=on_save,
    )  # fmt: skip
    return notice


def _pick_hint(opts: PlayOpts) -> str:
    """Discoverability line for the leaf lists: Tab flips the play mode, Alt-C casts,
    Ctrl-/ toggles the poster preview (wired in every preview-enabled menu)."""
    tab = "Tab: scegli sorgente/tracce" if opts.auto else "Tab: avvia al volo"
    return ui.key_hint(tab, "Alt-C: casta sul TV", "Ctrl-/: anteprima")


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


def run_browse(
    cfg: Config,
    cat: str,
    opts: PlayOpts,
    typ: str | None = None,
    *,
    genre: str | None = None,
) -> int:
    """Browse a Cinemeta catalog (typed or mixed), with optional genre filter and
    in-place pagination via a trailing «altri…» row when a full page is returned."""
    skip = 0
    header: str | None = None
    g = ui.glyphs(ui.active_caps())
    _MORE = object()  # sentinel: next page (not a Meta)
    while True:
        if typ:
            metas = api.catalog(cfg, typ, cat, genre=genre, skip=skip)
        else:
            metas = api.browse(cfg, cat, genre=genre, skip=skip)
        if not metas:
            if skip == 0:
                print("nstream: catalogo vuoto", file=sys.stderr)
                return 1
            # Past the last page after «altri…»: stay put isn't possible — leave.
            return 0

        items: list[tuple[str, Meta | object]] = [(meta_label(m), m) for m in metas]
        if len(metas) >= CATALOG_PAGE:
            items.append((f"{g.down}  altri…", _MORE))

        page_header = header
        if page_header is None and (genre or skip):
            bits = []
            if genre:
                bits.append(genre)
            if skip:
                bits.append(f"pagina {skip // CATALOG_PAGE + 1}")
            page_header = " · ".join(bits) if bits else None

        while True:
            chosen = fzf_key(
                items,
                "titolo> ",
                header=page_header or _pick_hint(opts),
                preview=lambda v: None if v is _MORE else _meta_preview(typecast("Meta", v)),
            )
            if not chosen:
                return 0
            key, value = chosen
            if value is _MORE:
                skip += CATALOG_PAGE
                header = None
                break  # outer loop fetches the next page
            meta = typecast("Meta", value)
            sel = replace(opts, cast=True, cast_choose=True) if key == "alt-c" else opts
            if meta.get("type") != "series":
                sel = _apply_key(opts, key)
            header = play_meta(cfg, meta, sel)
            page_header = header  # notice from playback on re-open of this page


def run_genre(cfg: Config, opts: PlayOpts, typ: str) -> int:
    """Pick a Cinemeta genre, then browse the typed `top` catalog filtered by it."""
    items = [(name, name) for name in GENRES]
    genre = fzf(items, "genere> ", header="catalogo Top filtrato per genere · ESC: indietro")
    if genre is None:
        return 0
    return run_browse(cfg, "top", opts, typ, genre=genre)


def run_explain(cfg: Config, query: str, opts: PlayOpts | None = None) -> int:
    """`--explain`: search → pick a title (and episode, for series) → print WHY the
    auto-pick won (ranking table for local + cast profiles, plus the audio decision).
    Read-only: never plays or casts. Honours `--quality` when set on `opts`."""
    exact = stream_select.exact_resolution(opts.quality if opts else None)
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
    print(explain.explain_streams(cfg, results, cast=False, title=title, exact_resolution=exact))
    print()
    print(explain.explain_streams(cfg, results, cast=True, title=title, exact_resolution=exact))
    print()
    print(
        explain.explain_audio(
            cfg,
            explain.auto_pick(cfg, results, cast=False, title=title, exact_resolution=exact),
        )
    )
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
_GENRE = "genre"
_SECTION = "section"
_SETTINGS = "settings"

# Section type → fzf prompt (home itself uses "nstream> ").
_SECTION_PROMPT = {"movie": "film> ", "series": "serie> "}


def _home_preview(value: object) -> str | None:
    """Continue-watching entries (dicts) get a poster pane; actions and group headers don't."""
    if not isinstance(value, dict):
        return None
    return _entry_preview(typecast("HistoryEntry", value))


def _ask_query() -> str | None:
    """Prompt for a search query inside fzf (keeps the TUI chrome). None on ESC/empty."""
    return ask_query("cerca> ")


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
        pal = ui.palette(ui.active_caps())
        items: list[tuple[str, object]] = [(history_label(e), e) for e in recent]
        # Dim section labels (value None) group the menu; the loop skips them if focused.
        # fzf still requires every row selectable — a None value is ignored after pick.
        _SEP = None  # sentinel: group headers, never an action
        if recent:
            items.append((ui.ansi("── azioni ──", pal.dim), _SEP))
        items.append((f"{g.search}  Cerca…", (_SEARCH, "")))
        if typ is None:  # home: the typed sections own the catalogs
            items += [
                (f"{g.movie}  Film", (_SECTION, "movie")),
                (f"{g.series}  Serie TV", (_SECTION, "series")),
                (ui.ansi("── sistema ──", pal.dim), _SEP),
                (f"{g.gear}  Impostazioni", (_SETTINGS, "")),
            ]
        else:  # section: catalogs + genre browse, served per-type by api.catalog
            items += [
                (f"{g.fire}  Popolari", (_BROWSE, "popolari")),
                (f"{g.new}  Novità", (_BROWSE, "nuovi")),
                (f"{g.star}  Top IMDb", (_BROWSE, "top")),
                (f"{g.folder}  Generi…", (_GENRE, "")),
            ]

        # The Tab hint only applies to the continue-watching rows.
        header = notice or (_pick_hint(opts) if recent else None)
        prompt = _SECTION_PROMPT.get(typ or "", "nstream> ")
        chosen = fzf_key(items, prompt, header=header, preview=_home_preview)
        notice = None
        if chosen is None:
            return 0
        key, value = chosen
        if value is _SEP:
            continue  # group header — re-open the menu
        if not isinstance(value, tuple):  # a continue-watching entry
            notice = play_history(cfg, typecast("HistoryEntry", value), _apply_key(opts, key))
            continue
        kind, value = value
        if kind == _SEARCH:
            query = _ask_query()
            if query is None:
                continue  # ESC on search → stay in home (not exit the whole TUI)
            if query:
                run_search(cfg, query, opts, typ)
        elif kind == _SECTION:
            run_section(cfg, typecast(str, value), opts)
        elif kind == _BROWSE:
            run_browse(cfg, CAT_MAP[typecast(str, value)], opts, typ)
        elif kind == _GENRE:
            run_genre(cfg, opts, typecast(str, typ))
        elif kind == _SETTINGS:
            settings.run_settings(cfg)
            cfg = load()  # pick up any change for the next loop


def _dispatch(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    if args.json:
        # Headless: no _clear, no fzf, JSON on stdout (incl. the NetworkError → JSON guard).
        return headless.run(cfg, args, opts)
    typ = headless.typ_filter(args)
    if not args.explain:
        # Warm device discovery + cache while the user browses, so a later cast resolves
        # instantly (--explain never casts; --json returned above and scans on demand).
        discovery.start_background()
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
        return run_explain(cfg, query, opts)
    if query:
        return run_search(cfg, query, opts, typ)
    return run_home(cfg, opts)  # the home has the typed sections; flags don't apply


def _parse_sub_fps(raw: str) -> float | None:
    """`--sub-fps SRC:DST` → retime scale (SRC/DST), or None when invalid. SRC = the fps
    the subtitle was authored for, DST = the video's fps: subs timed for 25 on a 23.976
    video must stretch by 25/23.976 (events land later in the slower-playing video)."""
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    try:
        src, dst = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if src <= 0 or dst <= 0:
        return None
    return src / dst


def _sub_options(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.sub_lang:
        return ("auto", args.sub_lang)
    if args.sub_menu:
        return ("menu", None)
    if args.subs:
        return ("auto", None)
    return (None, None)


def _json_error(error: str, message: str) -> None:
    """Emit the one JSON error object the `--json` contract promises on stdout, for
    failure paths that die before (or outside) `headless.run` — a missing/corrupt
    config, an unexpected crash. Without it an agent parsing stdout sees nothing."""
    sys.stdout.write(json.dumps({"ok": False, "error": error, "message": message}) + "\n")
    sys.stdout.flush()


def _ensure_config(*, headless_mode: bool = False) -> Config:
    """Load config, running first-run onboarding if it's missing. Headless (`--json`)
    never onboards: the wizard is an interactive fzf/getpass flow, which would hang an
    agent — it raises instead, and the caller emits the JSON error object."""
    try:
        return load()
    except ConfigError:
        if not config_path().exists():
            if headless_mode:
                raise ConfigError(
                    "config assente — esegui `nstream --settings` per l'onboarding"
                ) from None
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
        description=(
            "Native terminal-first Stremio-like client "
            "(Cinemeta + Torrentio/debrid or local P2P + mpv; cast via castbridge or catt)."
        ),
    )
    parser.add_argument("query", nargs="*", help="titolo da cercare (altrimenti chiede)")
    parser.add_argument(
        "--play",
        action="store_true",
        help="forza la riproduzione automatica (anche se disattivata)",
    )
    parser.add_argument(
        "--cast",
        action="store_true",
        help=(
            "manda lo stream a un Chromecast "
            "(castbridge se disponibile, altrimenti catt) invece di mpv"
        ),
    )
    parser.add_argument(
        "--mirror",
        action="store_true",
        help="cast via mirror nativo (mpv su output headless → sender): parte subito, 1080p SDR",
    )
    parser.add_argument(
        "--no-mirror",
        action="store_true",
        help="sopprimi il mirror per questa invocazione (anche l'auto-switch ADR-0015)",
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
        "--sub-offset", metavar="SEC", type=float, default=0.0,
        help="ritima i sottotitoli di ±SEC secondi (es. -2.5)",
    )  # fmt: skip
    parser.add_argument(
        "--sub-fps", metavar="SRC:DST",
        help="corregge il drift da framerate: fps per cui i sottotitoli sono stati creati"
        " : fps del video (es. 25:23.976)",
    )  # fmt: skip
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
        "--quality",
        metavar="RES",
        help=(
            "filtra per risoluzione esatta: auto, 720, 1080, 4k/2160 "
            "(TUI e --json; senza flag la TUI chiede, --json non filtra)"
        ),
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
        help="--json: volume del Chromecast (0-100); senza titolo agisce sul cast in corso",
    )
    parser.add_argument(
        "--pause", action="store_true", help="--json: mette in pausa il cast in corso"
    )
    parser.add_argument("--resume", action="store_true", help="--json: riprende il cast in pausa")
    parser.add_argument(
        "--seek",
        type=float,
        metavar="SEC",
        help="--json: salta alla posizione SEC (secondi) del cast in corso",
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
        cfg = _ensure_config(headless_mode=args.json)
    except ConfigError as e:
        if args.json:
            _json_error("config", str(e))
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
    if args.mirror and args.no_mirror:
        msg = "--mirror e --no-mirror sono incompatibili"
        if args.json:
            _json_error("usage", msg)
        print(f"nstream: {msg}", file=sys.stderr)
        return 2
    sub_scale = 1.0
    if getattr(args, "sub_fps", None):
        sub_scale = _parse_sub_fps(args.sub_fps)
        if sub_scale is None:
            msg = f"--sub-fps non valido: «{args.sub_fps}» (formato SRC:DST, es. 25:23.976)"
            if args.json:
                _json_error("usage", msg)
            print(f"nstream: {msg}", file=sys.stderr)
            return 2
    # Mirror is a cast backend: it implies cast routing (device resolution), unless local.
    # Tri-state (ADR 0021): True = forced (flag or cast_mode), False = --no-mirror,
    # None = no per-invocation preference (the ADR-0015 auto-switch may apply).
    mirror_opt: bool | None = None
    if (args.mirror or cfg.cast_mode == "mirror") and not args.local:
        mirror_opt = True
    elif args.no_mirror:
        mirror_opt = False
    quality: int | None = None
    if getattr(args, "quality", None):
        parsed = quality_mod.parse_quality(args.quality)
        if parsed is None:
            msg = f"qualità non valida: «{args.quality}» (usa auto, 720, 1080, 4k…)"
            if args.json:
                _json_error("usage", msg)
            print(f"nstream: {msg}", file=sys.stderr)
            return 2
        quality = parsed
    opts = PlayOpts(
        # --json is headless: always auto-pick (no fzf stream menu).
        auto=cfg.auto_play or args.play or args.json,
        cast=(cfg.prefer_cast or args.cast or mirror_opt is True) and not args.local,
        sub_mode=sub_mode,
        sub_lang=sub_lang,
        history=cfg.history_enabled and not args.no_history,
        autoplay=cfg.autoplay and not args.no_autoplay,
        audio_lang=args.audio_lang or None,
        mirror=mirror_opt,
        quality=quality,
        sub_offset=args.sub_offset or 0.0,
        sub_scale=sub_scale,
    )
    try:
        return _dispatch(cfg, args, opts)
    except api.NetworkError as e:
        # --json never lands here: headless.run has its own NetworkError → JSON guard.
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
        message = f"errore inatteso — dettagli in {log.log_path()}"
        if "--json" in sys.argv[1:]:  # pre-argparse: keep the JSON contract even on a crash
            _json_error("internal", message)
        print(f"nstream: {message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _entry()
