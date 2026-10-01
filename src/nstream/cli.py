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
    addons,
    api,
    application,
    cast_control,
    cast_flow,
    caster,
    cli_args,
    debrid,
    discovery,
    doctor,
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
from .api import CAT_MAP, CATALOG_PAGE, GENRES
from .caster import CastUnavailable
from .caster import resolve_device as _resolve_device
from .config import (
    Config,
    ConfigError,
    PlayOpts,
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
from .playback import PlaybackError
from .player import play
from .subs import auto_subs, choose_tracks  # re-export: tests + _play_on_mpv
from .types import (
    HistoryEntry,
    Meta,
    Stream,
)

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
    # Expected playtime of this video (0 = unknown → the truncation guard stays off, ADR
    # 0028); shared by the selection guard and every cast reselect below.
    expected_s = api.expected_runtime_s(cfg, typ, video_id)
    try:
        vetted = stream_select.prepare_stream(
            cfg,
            results,
            opts,
            auto=auto,
            reselect_on_wrong_audio=reselect_on_wrong_audio,
            title=title,
            expected_runtime_s=expected_s,
        )
    except stream_select.AudioLangUnavailable as e:
        have = ", ".join(e.available) or "—"
        notice = f"audio «{e.lang}» non disponibile (disponibili: {have})"
        ui.status(notice, kind="fail")
        print(f"nstream: {notice}", file=sys.stderr)
        return (notice, False, opts.quality if opts.quality is not None else 0)
    except stream_select.QualityUnavailable as e:
        have = ", ".join(f"{r}p" for r in e.available) or "—"
        notice = f"nessuno stream {e.quality}p (disponibili: {have})"
        ui.status(notice, kind="fail")
        print(f"nstream: {notice}", file=sys.stderr)
        return (notice, False, e.quality)
    except stream_select.NoPlayableStream as e:
        # The reason returns as the notice so it reaches the fzf header: stderr scrolls away
        # under the menu's fullscreen redraw, and a silent return reads as "nothing happened".
        ui.status(e.reason, kind="fail")
        return (e.reason, False, opts.quality if opts.quality is not None else 0)
    except stream_select.ContentTooShort as e:
        # Every probed source is a placeholder, not the video: say so instead of playing
        # 30 seconds of "removed for copyright". Retrying wouldn't change the file.
        notice = f"sorgenti troncate per «{title}» ({e.verdict.reason}) — prova un'altra qualità"
        ui.status(notice, kind="fail")
        return (notice, False, opts.quality if opts.quality is not None else 0)
    if vetted is None:
        # Backed out of a (re)selection / quality picker, or no playable without a hard tier.
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
                cast_meta=cast_meta, expected_runtime_s=expected_s,
            )  # fmt: skip
        else:
            try:
                res = _play_on_mpv(
                    cfg, results, chosen, work_dir,
                    typ=typ, video_id=video_id, title=title, opts=opts,
                    start=start, next_label=next_label, auto=auto, safety_sub_lang=safety_sub_lang,
                    cast_meta=cast_meta, expected_runtime_s=expected_s,
                )  # fmt: skip
            except PlaybackError as e:
                ui.status(str(e), kind="fail")
                return (str(e), False, quality_choice)
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
    expected_runtime_s: float = 0.0,
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
            expected_runtime_s=expected_runtime_s,
        )  # fmt: skip
    except cast_flow.CastStreamUnresolved:
        # Same class as the black-screen guard below: back out to the list rather than
        # crashing on a url-less stream (ADR 0031 appendix).
        print(
            f"nstream: {ui.g().warn} nessuna sorgente castabile risolvibile ora — "
            "riprova più tardi o scegli un'altra release",
            file=sys.stderr,
        )
        return (0.0, 0.0, False)
    except cast_flow.CastRemuxInfeasible as e:
        # Casting the undecodable file anyway plays mute: back out with the reason.
        print(f"nstream: {ui.g().warn} cast annullato — {e}", file=sys.stderr)
        return (0.0, 0.0, False)
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
    results: list[Stream],
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
    cast_meta: caster.CastMeta | None = None,
    expected_runtime_s: float = 0.0,
) -> tuple[float, float, bool] | None:
    """Play locally in mpv. Returns (pos, dur, advance), or None if the user backed
    out of the pre-play track menu (so the caller returns to the list)."""
    result = application.play_local(
        cfg,
        application.LocalRequest(
            title,
            typ,
            video_id,
            chosen,
            opts,
            work_dir,
            start=start,
            next_label=next_label,
            cast_enabled=shutil.which("catt") is not None,
            auto=auto,
            safety_sub_lang=safety_sub_lang,
        ),
        backend=play,
        acquire_subs=auto_subs,
        choose_tracks=choose_tracks,
    )
    if result is None:
        return None
    pos, dur, signal = result.playback
    if signal == "cast":  # Alt-C in mpv: move this playback to the TV from `pos`
        return _move_to_cast(
            cfg, results, chosen, work_dir, title, pos, dur,
            typ=typ, video_id=video_id, opts=opts, safety_sub_lang=safety_sub_lang,
            cast_meta=cast_meta, expected_runtime_s=expected_runtime_s,
        )  # fmt: skip
    return (pos, dur, signal == "next")


def _move_to_cast(
    cfg: Config,
    results: list[Stream],
    chosen: Stream,
    work_dir: str,
    title: str,
    pos: float,
    dur: float,
    *,
    typ: str,
    video_id: str,
    opts: PlayOpts,
    safety_sub_lang: str | None = None,
    cast_meta: caster.CastMeta | None = None,
    expected_runtime_s: float = 0.0,
) -> tuple[float, float, bool]:
    """Hand the running mpv position over to a Chromecast (in-player Alt-C).

    Goes through `cast_flow.run_cast` so cast_vet (video/container/audio) and Tier-2
    remux/mirror apply — the local pick was ranked for the GPU, not the DMR. Never
    auto-advances (the user is switching destination mid-play)."""
    try:
        device = _resolve_device(cfg, choose=True)
    except CastUnavailable as e:
        print(f"nstream: {e}", file=sys.stderr)
        return (pos, dur, False)
    # Full cast decision tree from the current position (not a raw catt cast of the URL).
    cast_opts = replace(opts, cast=True, cast_choose=True)
    try:
        outcome = cast_flow.run_cast(
            cfg, results, chosen,
            device=device, title=title, typ=typ, video_id=video_id, work_dir=work_dir,
            opts=cast_opts, start=pos, follow=True, next_label=None,
            allow_lang_switch=True, meta=cast_meta, safety_sub_lang=safety_sub_lang,
            expected_runtime_s=expected_runtime_s,
        )  # fmt: skip
    except cast_flow.CastStreamUnresolved:
        print(
            f"nstream: {ui.g().warn} nessuna sorgente castabile risolvibile ora — resto in locale",
            file=sys.stderr,
        )
        return (pos, dur, False)
    except cast_flow.CastRemuxInfeasible as e:
        print(f"nstream: {ui.g().warn} {e} — resto in locale", file=sys.stderr)
        return (pos, dur, False)
    except cast_flow.CastVideoUnsupported as e:
        print(
            f"nstream: {ui.g().warn} video {e.codec.upper()} non decodificabile dal TV — "
            "resto in locale",
            file=sys.stderr,
        )
        return (pos, dur, False)
    return (outcome.pos, outcome.dur, False)


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
    return ui.key_hint(tab, "Alt-C: casta sul TV", "Alt-W: watchlist", "Ctrl-/: anteprima")


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
            items,
            "titolo> ",
            header=header or _pick_hint(opts),
            expect=("tab", "alt-c", "alt-w"),
            preview=_meta_preview,
        )
        if not chosen:
            return 0
        key, meta = chosen
        if key == "alt-w":
            enabled = state.toggle_watchlist(cfg, meta)
            header = "aggiunto alla watchlist" if enabled else "rimosso dalla watchlist"
            continue
        # Series defer auto/manual to the episode picker, but Alt-C (cast) still applies.
        sel = replace(opts, cast=True, cast_choose=True) if key == "alt-c" else opts
        if meta.get("type") != "series":
            sel = _apply_key(opts, key)
        header = play_meta(cfg, meta, sel)


def _meta_rows(cfg: Config, metas: list[Meta]) -> list[tuple[str, Meta]]:
    """Label catalog rows with a watchlist star when the title is already saved."""
    return [
        (meta_label(m, watchlisted=state.is_watchlisted(cfg, m.get("id") or "")), m) for m in metas
    ]


def run_search(cfg: Config, query: str, opts: PlayOpts, typ: str | None = None) -> int:
    if opts.history:
        state.remember_search(cfg, query)
    metas = api.search(cfg, query, typ)
    if not metas:
        print(
            "nstream: nessun risultato — prova un'altra query o Impostazioni → Fonti",
            file=sys.stderr,
        )
        return 1
    return _pick_meta(_meta_rows(cfg, metas), cfg, opts)


def run_watchlist(cfg: Config, opts: PlayOpts, typ: str | None = None) -> int:
    """Play or remove locally saved titles without contacting a catalog addon."""
    metas = state.watchlist(cfg)
    if typ:
        metas = [m for m in metas if m.get("type", "movie") == typ]
    if not metas:
        print("nstream: watchlist vuota", file=sys.stderr)
        return 0
    header: str | None = "Alt-W: aggiungi/rimuovi dalla watchlist"
    while True:
        chosen = fzf_key(
            _meta_rows(cfg, metas),
            "watchlist> ",
            header=header,
            expect=("tab", "alt-c", "alt-w"),
            preview=_meta_preview,
        )
        if not chosen:
            return 0
        key, meta = chosen
        if key == "alt-w":
            state.toggle_watchlist(cfg, meta)
            metas = [m for m in metas if m.get("id") != meta.get("id")]
            if not metas:
                return 0
            header = "rimosso dalla watchlist"
            continue
        header = play_meta(cfg, meta, _apply_key(opts, key))


def run_recent_searches(cfg: Config, opts: PlayOpts, typ: str | None = None) -> int:
    queries = state.recent_searches(cfg)
    if not queries:
        print("nstream: nessuna ricerca recente", file=sys.stderr)
        return 0
    query = fzf([(q, q) for q in queries], "ricerche> ", header="INVIO: cerca · ESC: indietro")
    if query is None:
        return 0
    return run_search(cfg, query, opts, typ)


def run_browse(
    cfg: Config,
    cat: str,
    opts: PlayOpts,
    typ: str | None = None,
    *,
    genre: str | None = None,
) -> int:
    """Browse a catalog id (Cinemeta or user-addon), typed or mixed, with optional
    genre filter and in-place pagination via a trailing «altri…» row when a full
    page is returned."""
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
                print(
                    "nstream: catalogo vuoto — controlla rete o Impostazioni → Fonti",
                    file=sys.stderr,
                )
                return 1
            # Past the last page after «altri…»: stay put isn't possible — leave.
            return 0

        items: list[tuple[str, Meta | object]] = list(_meta_rows(cfg, metas))
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
                expect=("tab", "alt-c", "alt-w"),
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
            if key == "alt-w":
                enabled = state.toggle_watchlist(cfg, meta)
                header = "aggiunto alla watchlist" if enabled else "rimosso dalla watchlist"
                page_header = header
                continue
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
    meta = fzf(_meta_rows(cfg, metas), "titolo> ", preview=_meta_preview)
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
    `typ` (--movies/--series) narrows the list to one content type.

    The list is `state.resumable` (ADR 0029), so a finished episode stays here as "→ prossimo
    episodio" instead of vanishing: the headless `-c` has always advanced past the credits,
    and the TUI hiding the series was the asymmetry, not a feature."""
    entries = state.resumable(cfg, typ=typ)
    if not entries:
        print("nstream: cronologia vuota", file=sys.stderr)
        return 0
    header: str | None = None
    while True:
        # `is_watched` is O(1) per row; resolving the actual next episode would cost one
        # `api.episodes` per row, so that happens only once a row is chosen.
        items = [(history_label(e, next_episode=state.is_watched(e)), e) for e in entries]
        chosen = fzf_key(
            items, "continua> ", header=header or _pick_hint(opts), preview=_entry_preview
        )
        if chosen is None:
            return 0
        key, entry = chosen
        header = play_history(cfg, entry, _apply_key(opts, key))
        entries = state.resumable(cfg, typ=typ)  # reflect updated positions, then re-show


# Home-menu action kinds (the value half of an fzf item; history entries are dicts).
_SEARCH = "search"
_RECENT_SEARCH = "recent_search"
_WATCHLIST = "watchlist"
_BROWSE = "browse"
_GENRE = "genre"
_SECTION = "section"
_SETTINGS = "settings"
_CAST_LIVE = "cast_live"
_MORE_CONTINUE = "more_continue"
_HELP = "help"

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
    """A type-scoped home section: type-filtered continue-watching, search, the three
    Cinemeta catalogs, and any extra catalogs declared by user addons — all pinned
    to `typ`. ESC returns to the home menu."""
    return _home_menu(cfg, opts, typ=typ)


def _cast_live_menu(cfg: Config) -> str | None:
    """Submenu for the active cast session: status / pause / seek / volume / stop."""
    session = state.cast_session_info()
    if not session:
        return "nessun cast attivo"
    g = ui.glyphs(ui.active_caps())
    device = session.get("device") or ""
    items = [
        (f"{g.tv}  Stato ricevitore", "status"),
        (f"{g.tv}  Aggiorna posizione", "refresh"),
        (f"{g.play}  Pausa", "pause"),
        (f"{g.play}  Riprendi", "play"),
        (f"{g.down}  Seek −30s", "seek-30"),
        (f"{g.down}  Seek +30s", "seek+30"),
        (f"{g.down}  Seek +5 min", "seek+300"),
        (f"{g.audio}  Volume 35%", "vol35"),
        (f"{g.audio}  Volume 50%", "vol50"),
        (f"{g.audio}  Volume 70%", "vol70"),
        (f"{g.fail}  Ferma cast", "stop"),
    ]
    header = state.cast_session_label(session) or "cast"
    pick = fzf(items, "cast> ", header=header)
    if pick is None:
        return None
    if pick == "status":
        cast_control.refresh_session_from_status(cfg, device=device or None)
        ok, msg = cast_control.cast_status(device=device or None)
        ui.status(msg, kind="tv" if ok else "fail")
        return msg
    if pick == "refresh":
        cast_control.refresh_session_from_status(cfg, device=device or None)
        ok, msg = cast_control.cast_status(device=device or None)
        ui.status(msg or "posizione aggiornata", kind="tv" if ok else "fail")
        return msg
    if pick in ("pause", "play"):
        ok, msg = cast_control.media_control(pick, device=device or None)
        ui.status(msg, kind="tv" if ok else "fail")
        return msg
    if pick.startswith("seek"):
        # Relative seek: read current position then apply delta (bridge seek is absolute).
        delta = int(pick.removeprefix("seek"))
        st = caster.status(device) if device else {}
        pos = float(st.get("position") or 0.0)
        target = max(0.0, pos + delta)
        ok, msg = cast_control.media_control("seek", value=target, device=device or None)
        ui.status(msg, kind="tv" if ok else "fail")
        return msg
    if pick.startswith("vol"):
        level = int(pick.removeprefix("vol"))
        ok, msg = cast_control.set_cast_volume(level, device=device or None)
        ui.status(msg, kind="audio" if ok else "fail")
        return msg
    if pick == "stop":
        ok, msg = cast_control.stop_cast(cfg, device=device or None)
        ui.status(msg, kind="tv" if ok else "fail")
        return msg
    return None


def _home_help() -> str:
    """Static keybinding help for the home menu header."""
    return ui.key_hint(
        "Tab: manuale",
        "Alt-C: cast",
        "Alt-W: watchlist",
        "Ctrl-/: anteprima",
        "ESC: indietro",
    )


def _home_menu(cfg: Config, opts: PlayOpts, *, typ: str | None) -> int:
    """Shared loop behind run_home (typ None: mixed rows + sections + settings) and
    run_section (typ set: rows and catalogs pinned to one type)."""
    notice: str | None = None
    show_all_continue = False
    while True:
        state.expire_cast_session()
        recent = state.resumable(cfg, typ=typ) if opts.history else []
        cap = cfg.home_continue_max
        truncated = False
        if not show_all_continue and cap > 0 and len(recent) > cap:
            recent_view = recent[:cap]
            truncated = True
        else:
            recent_view = recent
        g = ui.glyphs(ui.active_caps())
        pal = ui.palette(ui.active_caps())
        items: list[tuple[str, object]] = []
        # Live cast session first — discoverable stop/status without --json.
        # Shown on home and typed sections (cast is global, not type-scoped).
        sess = state.cast_session_info()
        label = state.cast_session_label(sess)
        if label:
            items.append((f"{g.tv}  {label}", (_CAST_LIVE, "")))
        items += [(history_label(e, next_episode=state.is_watched(e)), e) for e in recent_view]
        if truncated:
            n_more = len(recent) - len(recent_view)
            items.append((f"{g.down}  …altri {n_more} in continua", (_MORE_CONTINUE, "")))
        # Dim section labels (value None) group the menu; the loop skips them if focused.
        # fzf still requires every row selectable — a None value is ignored after pick.
        _SEP = None  # sentinel: group headers, never an action
        if items:
            items.append((ui.ansi("── azioni ──", pal.dim), _SEP))
        items.append((f"{g.search}  Cerca…", (_SEARCH, "")))
        if state.recent_searches(cfg):
            items.append((f"{g.search}  Ricerche recenti", (_RECENT_SEARCH, "")))
        if state.watchlist(cfg):
            items.append((f"{g.star}  Watchlist", (_WATCHLIST, "")))
        if typ is None:  # home: the typed sections own the catalogs
            items += [
                (f"{g.movie}  Film", (_SECTION, "movie")),
                (f"{g.series}  Serie TV", (_SECTION, "series")),
                (ui.ansi("── sistema ──", pal.dim), _SEP),
                (f"{g.gear}  Impostazioni", (_SETTINGS, "")),
                (f"{g.play}  Aiuto tasti", (_HELP, "")),
            ]
        else:  # section: Cinemeta catalogs + genre + any user-addon catalogs
            # _BROWSE values are catalog *ids* (top/year/imdbRating/…), not --browse keywords.
            items += [
                (f"{g.fire}  Popolari", (_BROWSE, "top")),
                (f"{g.new}  Novità", (_BROWSE, "year")),
                (f"{g.star}  Top IMDb", (_BROWSE, "imdbRating")),
                (f"{g.folder}  Generi…", (_GENRE, "")),
            ]
            extras = addons.extra_catalogs(cfg, typ)
            if extras:
                items.append((ui.ansi("── cataloghi addon ──", pal.dim), _SEP))
                items += [(f"{g.folder}  {label}", (_BROWSE, cat_id)) for cat_id, label in extras]

        # Prefer an explicit notice; else key hints (always useful on home).
        header = notice or _home_help()
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
        if kind == _CAST_LIVE:
            notice = _cast_live_menu(cfg)
        elif kind == _MORE_CONTINUE:
            show_all_continue = True
        elif kind == _HELP:
            notice = _home_help()
        elif kind == _SEARCH:
            query = _ask_query()
            if query is None:
                continue  # ESC on search → stay in home (not exit the whole TUI)
            if query:
                run_search(cfg, query, opts, typ)
        elif kind == _RECENT_SEARCH:
            run_recent_searches(cfg, opts, typ)
        elif kind == _WATCHLIST:
            run_watchlist(cfg, opts, typ)
        elif kind == _SECTION:
            run_section(cfg, typecast(str, value), opts)
        elif kind == _BROWSE:
            run_browse(cfg, typecast(str, value), opts, typ)
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
    sys.stdout.write(
        json.dumps({"ok": False, "error": error, "message": log.redact(message)}) + "\n"
    )
    sys.stdout.flush()


def _emit_forget_dead(dropped: int, message: str) -> None:
    """`--json --forget-dead`: same one-object contract as every other headless command."""
    payload = {"ok": True, "action": "forget_dead", "removed_sources": dropped, "message": message}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _emit_forget_breakers(dropped: int, message: str) -> None:
    """`--json --forget-breakers`: clear per-addon circuit breakers (ADR 0027)."""
    payload = {
        "ok": True,
        "action": "forget_breakers",
        "removed_breakers": dropped,
        "message": message,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
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

    parser = cli_args.build_parser()
    args = parser.parse_args()

    if args.doctor:
        return doctor.run(json_mode=args.json)

    log.setup_logging(args.debug or bool(os.environ.get("NSTREAM_DEBUG")))
    _log.info("nstream %s avvio (cast=%s)", __version__, args.cast or "")
    _log.debug("mode: json=%s cast=%s", args.json, args.cast)

    try:
        cfg = _ensure_config(headless_mode=args.json)
    except ConfigError as e:
        if args.json:
            _json_error("config", str(e))
        print(f"nstream: {e}", file=sys.stderr)
        return 2

    _init_theme(cfg)

    # Headless-only flags without --json used to be silent no-ops (home / wrong play).
    misuse = cli_args.headless_only_misuse(args)
    if misuse:
        if args.json:  # unreachable today; keep the JSON contract if that changes
            _json_error("usage", misuse)
        print(f"nstream: {misuse}", file=sys.stderr)
        return 2

    if args.settings:
        if args.json:
            # Settings is an interactive fzf flow — never hang an agent on a TTY prompt.
            _json_error("usage", "--settings è interattivo e incompatibile con --json")
            return 2
        settings.run_settings(cfg)
        return 0

    if args.debrid_test:
        print(debrid.selftest(cfg, args.debrid_test))
        return 0

    if args.forget_dead:
        dropped = state.forget_dead()
        msg = f"elenco sorgenti rimosse svuotato ({dropped} voci)"
        if args.json:
            _emit_forget_dead(dropped, msg)
        else:
            print(f"nstream: {msg}")
        return 0

    if getattr(args, "forget_breakers", False):
        dropped = state.forget_breakers()
        msg = f"circuit breaker addon azzerati ({dropped} voci)"
        if args.json:
            _emit_forget_breakers(dropped, msg)
        else:
            print(f"nstream: {msg}")
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
    if "--doctor" not in sys.argv[1:]:
        log.setup_logging(bool(os.environ.get("NSTREAM_DEBUG")))
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        if "--json" in sys.argv[1:]:
            _json_error("cancelled", "operazione annullata")
        sys.exit(130)
    except PlaybackError as e:
        if "--json" in sys.argv[1:]:
            _json_error(e.code, str(e))
        print(f"nstream: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        _log.exception("crash non gestito")
        message = f"errore inatteso — dettagli in {log.log_path()}"
        if "--json" in sys.argv[1:]:  # pre-argparse: keep the JSON contract even on a crash
            _json_error("internal", message)
        print(f"nstream: {message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _entry()
