"""Command-line entry point: search/browse → pick (fzf) → play (mpv)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import replace
from typing import cast as typecast

from . import (
    __version__,
    api,
    bridge,
    caster,
    debrid,
    explain,
    log,
    mirror,
    preview,
    quality,
    remux,
    settings,
    state,
    stream_select,
    tracks,
    ui,
)
from .caster import CastUnavailable, cast, device_volume
from .caster import resolve_device as _resolve_device
from .config import (
    Config,
    ConfigError,
    HistoryEntry,
    Meta,
    PlayOpts,
    Stream,
    Video,
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
from .subs import auto_subs, available_subtitle_langs, pick_subtitles

# --browse keyword → Cinemeta catalog id.
CAT_MAP = {"popolari": "top", "nuovi": "year", "top": "imdbRating"}

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
            (f"{ui.glyphs(ui.active_caps()).play}  Avvia", _PLAY),
            (f"🔊 Audio: {audio_summary(aid, tr)}", _AUDIO),
            (f"💬 Sottotitoli: {sub_summary(sid, sub_paths, tr)}", _SUBS),
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


def _resume_position(cfg: Config, video_id: str) -> float | None:
    """The position to resume from, or None if there's no usable resume point
    (no history entry, or the title is effectively finished — so we never restart
    at the very end when mpv was left paused at EOF with keep-open)."""
    entry = state.load_history(cfg).get(video_id)
    if not entry or state._watched(entry):
        return None
    start = entry.get("position")
    dur = entry.get("duration") or 0.0
    if start and dur > 0:
        return min(start, dur - 5)
    return start


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
    device = _resolve_cast_device(cfg, opts) if opts.cast else None
    if opts.cast and device is None:
        opts = replace(opts, cast=False)  # degrade: select and play with the local profile
    # Resolving streams (Torrentio + RD) can take a moment; without a menu to mask
    # the wait, say what's happening so the TUI doesn't look frozen.
    print(f"▶ {title} — cerco la sorgente migliore…", file=sys.stderr)
    results = api.streams(cfg, typ, video_id)
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
        start = _resume_position(cfg, video_id) if opts.history else None
        name_line = next(iter((chosen.get("name") or "").splitlines()), "")
        if device is not None:
            print(f"▶ {title} — {name_line}", file=sys.stderr)
            pos, dur, advance = _play_on_cast(
                cfg, results, chosen, work_dir, device,
                typ=typ, video_id=video_id, title=title, opts=opts,
                start=start, next_label=next_label, safety_sub_lang=safety_sub_lang,
                cast_meta=cast_meta,
            )  # fmt: skip
        else:
            print(f"▶ {title} — {name_line}", file=sys.stderr)
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
    LAN has no reachable device (e.g. after a network change)."""
    try:
        return _resolve_device(cfg, choose=opts.cast_choose)
    except CastUnavailable as e:
        print(f"nstream: {e} — riproduco in locale", file=sys.stderr)
        _log.info("nessun Chromecast → fallback locale")
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
    """Cast the chosen stream in the primary audio language. The Default Media Receiver plays
    a file's first audio track and can't switch tracks, so the language is enforced at selection
    time (`vet_cast_audio`): cast directly when the first track is already primary+decodable,
    remux to select the primary track otherwise, or reselect a dub that has it. Still offers the
    in-cast audio switch (re-cast a differently-dubbed release) when several dubs exist."""
    target_lang = opts.audio_lang or cfg.primary
    plan = stream_select.vet_cast_audio(cfg, results, chosen, target_lang)
    chosen = plan.stream
    # No dub carries the primary language: cast the best pick anyway, with primary-language
    # subtitles as a safety net (mirrors the local guard).
    if plan.mode == "absent" and target_lang:
        safety_sub_lang = target_lang
        print(
            f"nstream: audio {target_lang} non disponibile"
            + (f" (casto {plan.real_lang})" if plan.real_lang else "")
            + f"; sottotitoli {target_lang} attivati",
            file=sys.stderr,
        )
    sub_paths = auto_subs(cfg, typ, video_id, work_dir, opts, safety_sub_lang=safety_sub_lang)
    _log.info("cast '%s' → %s (%s/%s)", title, device, plan.mode, plan.real_lang or "?")
    # Backend strategy. The DMR plays AAC/HEVC/4K/HDR natively and instantly — strictly better
    # than the mirror (1080p SDR re-encode, latency) — so `--mirror` only actually mirrors when
    # the audio is one the DMR can't decode (plan.mode == "remux"): there mirroring (mpv decodes
    # Dolby/DTS locally → instant) beats the remux prepare-wait. For decodable audio, mirror is
    # transparently downgraded to the direct cast.
    if plan.mode == "remux":
        if opts.mirror and mirror.available():
            return mirror.cast_via_mirror(
                cfg, title, chosen["url"], device=device, start=start, sub_paths=sub_paths
            )
        remux_path = remux.remux_for_cast(
            chosen["url"], cfg,
            audio_index=plan.audio_index, size_gb=quality.parse_stream(chosen).size_gb,
        )  # fmt: skip
        if remux_path:
            return remux.cast_file(
                cfg, title, remux_path,
                device=device, start=start, sub_paths=sub_paths, follow=True,
                meta=cast_meta,
            )  # fmt: skip
        # remux refused (size guard) or failed → degrade to a direct cast of the same pick
    elif opts.mirror:
        print(
            "nstream: audio decodificabile dal TV → cast diretto nativo (mirror non necessario)",
            file=sys.stderr,
        )
    cast_langs = stream_select.cast_languages(cfg, results)
    return cast(
        cfg, title, chosen["url"],
        device=device, start=start, sub_paths=sub_paths, next_label=next_label,
        langs=cast_langs if len(cast_langs) > 1 else (),
        resolve_lang=stream_select.cast_resolver(cfg, results) if len(cast_langs) > 1 else None,
        meta=cast_meta,
    )  # fmt: skip


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


def _play_series(
    cfg: Config,
    series_id: str,
    name: str,
    eps: list[Video],
    start_video: Video,
    opts: PlayOpts,
    poster: str = "",
) -> str | None:
    """Play a series from `start_video`, auto-advancing through the overlay.
    Returns a notice (e.g. an episode with no streams) to surface, or None."""
    idx = next((i for i, v in enumerate(eps) if v.get("id") == start_video.get("id")), None)
    if idx is None:
        return None
    auto = opts.auto  # the first episode honours --play; binge episodes auto-pick
    binge = False  # True once we're auto-advancing unattended (no blocking reselection)
    while 0 <= idx < len(eps):
        video = eps[idx]
        video_id = video["id"]
        nxt = eps[idx + 1] if idx + 1 < len(eps) else None
        next_label = display_title(name, nxt) if (opts.autoplay and nxt is not None) else None

        def on_save(pos: float, dur: float, vid: str = video_id, v: Video = video) -> None:
            state.save_entry(
                cfg, state.make_entry(vid, name, "series", pos, dur, series_id=series_id, video=v)
            )

        notice, advance = _play_video(
            cfg, "series", video_id, display_title(name, video), opts,
            auto=auto, next_label=next_label, on_save=on_save,
            reselect_on_wrong_audio=not binge,  # binge advances warn-and-proceed, don't block
            cast_meta=caster.CastMeta(
                poster=poster, series_title=name,
                season=video.get("season", 0) or 0, episode=video.get("episode", 0) or 0,
            ),
        )  # fmt: skip
        if notice:
            return notice
        if not advance or nxt is None:
            return None
        idx += 1
        auto = True
        binge = True
        print(f"▶ Carico {display_title(name, eps[idx])}…", file=sys.stderr)
    return None


def play_meta(cfg: Config, meta: Meta, opts: PlayOpts) -> str | None:
    """Play a title; returns a notice to show above the list, or None."""
    typ = meta.get("type", "movie")
    name = meta.get("name", "nstream")
    if typ != "series":
        movie_id = meta["id"]

        def on_save(pos: float, dur: float) -> None:
            state.save_entry(cfg, state.make_entry(movie_id, name, typ, pos, dur))

        notice, _ = _play_video(
            cfg, typ, movie_id, display_title(name, None), opts,
            auto=opts.auto, next_label=None, on_save=on_save,
            cast_meta=caster.CastMeta(poster=meta.get("poster") or ""),
        )  # fmt: skip
        return notice

    eps = api.episodes(cfg, meta["id"])
    if not eps:
        return f"nessun episodio per «{name}»"
    items = [(episode_label(v), v) for v in eps]
    sid = meta["id"]

    def ep_preview(v: Video) -> str:
        return f"episode {sid} {v.get('season', 0)} {v.get('episode', 0)}"

    # Loop the episode picker so finishing/backing out returns here, not to the list.
    header: str | None = None
    while True:
        chosen = fzf_key(items, "episodio> ", header=header or _pick_hint(opts), preview=ep_preview)
        if not chosen:
            return None
        key, start_video = chosen
        header = _play_series(
            cfg, meta["id"], name, eps, start_video, _apply_key(opts, key),
            poster=meta.get("poster") or "",
        )  # fmt: skip


def _entry_video(entry: HistoryEntry) -> Video | None:
    if entry.get("type") != "series":
        return None
    return {"season": entry.get("season", 0), "episode": entry.get("episode", 0)}


def play_history(cfg: Config, entry: HistoryEntry, opts: PlayOpts) -> str | None:
    """Resume from a history entry; returns a notice to show, or None."""
    typ = entry.get("type", "movie")
    name = entry.get("title", "nstream")
    series_id = entry.get("series_id", "")
    # Resume a series and keep bingeing the rest of the season.
    if typ == "series" and series_id and opts.autoplay:
        eps = api.episodes(cfg, series_id)
        cur = next((v for v in eps if v.get("id") == entry["video_id"]), None)
        if cur is not None:
            return _play_series(cfg, series_id, name, eps, cur, opts)

    video_id = entry["video_id"]

    def on_save(pos: float, dur: float) -> None:
        state.save_entry(
            cfg,
            state.make_entry(
                video_id,
                name,
                typ,
                pos,
                dur,
                series_id=series_id,
                season=entry.get("season", 0),
                episode=entry.get("episode", 0),
            ),  # fmt: skip
        )

    notice, _ = _play_video(
        cfg, typ, video_id, display_title(name, _entry_video(entry)), opts,
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


def run_search(cfg: Config, query: str, opts: PlayOpts) -> int:
    metas = api.search(cfg, query)
    if not metas:
        print("nstream: nessun risultato", file=sys.stderr)
        return 1
    return _pick_meta([(meta_label(m), m) for m in metas], cfg, opts)


def run_browse(cfg: Config, cat: str, opts: PlayOpts) -> int:
    metas = api.browse(cfg, cat)  # movies + series, fetched concurrently
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


# --- headless (--json): non-interactive play/cast for agents ---------------


def _emit_json(obj: dict) -> None:
    """Write one machine-readable JSON object to stdout (progress stays on stderr).
    Never carries a stream/debrid url or token — only descriptive metadata."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _norm_title(s: str) -> str:
    """Casefold + strip punctuation for tolerant title matching."""
    return " ".join("".join(c if c.isalnum() else " " for c in s.casefold()).split())


def _select_meta(metas: list[Meta], query: str, year: str | None) -> tuple[Meta, str]:
    """Pick a title without fzf: an exact normalized-name match (optionally pinned by
    release year) wins, else the first result. Returns (meta, "exact"|"first")."""
    q = _norm_title(query)
    # A trailing 4-digit year ("dune 2021") is a disambiguator, not part of the title: split it
    # off so the name match works, and let it pin the year when --year wasn't given.
    head, _, tail = q.rpartition(" ")
    if head and len(tail) == 4 and tail.isdigit():
        q, year = head, year or tail
    exact = [m for m in metas if _norm_title(m.get("name", "")) == q]
    if year:
        by_year = [m for m in exact if str(m.get("releaseInfo", "")).startswith(year)]
        exact = by_year or exact
    if exact:
        return exact[0], "exact"
    return metas[0], "first"


def _stream_block(cfg: Config, chosen: Stream) -> dict:
    """Descriptive JSON for the chosen stream — parsed quality only, never the url/token."""
    info = quality.parse_stream(chosen)
    backend = (
        "debrid"
        if info.cached
        else {"local": "p2p", "auto": "p2p", "native": "native"}.get(
            cfg.playback_backend, cfg.playback_backend
        )
    )
    return {
        "resolution": info.resolution,
        "codec": info.codec,
        "audio": info.audio,
        "size_gb": round(info.size_gb, 2),
        "cached": info.cached,
        "languages": sorted(info.languages),
        "backend": backend,
    }


def run_auto(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    """`--json`: headless, non-interactive play/cast. No fzf, no TTY. Emits one JSON
    object on stdout (diagnostics on stderr). Returns 0 on success, 1 on a recoverable
    failure (no result / no stream / device), 2 on a usage error."""
    if args.sub_menu:
        _emit_json(
            {"ok": False, "error": "usage", "message": "--sub-menu incompatibile con --json"}
        )
        return 2
    # Cast lifecycle actions: no title needed, no playback.
    if args.stop:
        return _run_stop(cfg, args)
    if args.status:
        return _run_status(cfg, args)
    query = " ".join(args.query)
    if args.cont:
        return _run_auto_resume(cfg, args, opts, query)

    # Meta source: a catalog (--browse) or a title search.
    if args.browse:
        metas = api.browse(cfg, CAT_MAP[args.browse])
        if not metas:
            _emit_json({"ok": False, "error": "no_result", "message": "catalogo vuoto"})
            return 1
        meta, selection = metas[0], "browse"
    else:
        if not query:
            _emit_json({"ok": False, "error": "usage", "message": "--json richiede un titolo"})
            return 2
        metas = api.search(cfg, query)
        if not metas:
            _emit_json(
                {"ok": False, "error": "no_result", "message": f"nessun risultato per «{query}»"}
            )
            return 1
        meta, selection = _select_meta(metas, query, args.year)
    typ = meta.get("type", "movie")
    name = meta.get("name", "?")
    imdb_id = meta.get("id", "")
    season: int | None = None
    episode: int | None = None
    video_id = imdb_id
    title = display_title(name, None)

    if typ == "series":
        eps = api.episodes(cfg, imdb_id)
        season = args.season or 1
        episode = args.episode or 1
        v = next(
            (e for e in eps if e.get("season") == season and e.get("episode") == episode), None
        )
        if v is None:
            avail = sorted({(e.get("season", 0), e.get("episode", 0)) for e in eps})
            _emit_json(
                {
                    "ok": False,
                    "error": "episode_not_found",
                    "message": f"S{season:02d}E{episode:02d} non trovato per «{name}»",
                    "available": [{"season": s, "episode": ep} for s, ep in avail[:50]],
                }
            )
            return 1
        video_id = v["id"]
        title = display_title(name, v)

    if args.probe:
        # Discovery only: list available audio/subtitle languages, never play.
        results = api.streams(cfg, typ, video_id)
        _emit_json(
            {
                "ok": True,
                "action": "probe",
                "title": title,
                "type": typ,
                "imdb_id": imdb_id,
                "season": season,
                "episode": episode,
                "available_audio": list(
                    stream_select.audio_languages(cfg, results, cast=opts.cast)
                ),
                "available_subtitles": available_subtitle_langs(cfg, typ, video_id),
                "error": None,
            }
        )
        return 0

    # Now-playing metadata for the castbridge LOAD (TV card + HUD widget). Poster is the public
    # Cinemeta image URL; for a series the show name is the series title and `title` the episode.
    poster = meta.get("poster") or ""
    if typ == "series":
        cast_meta = caster.CastMeta(
            poster=poster, series_title=name, season=season or 0, episode=episode or 0
        )
    else:
        cast_meta = caster.CastMeta(poster=poster)
    return _auto_play(
        cfg, args, opts, typ, video_id, title, imdb_id, season, episode, selection, cast_meta
    )


def _auto_play(
    cfg: Config,
    args: argparse.Namespace,
    opts: PlayOpts,
    typ: str,
    video_id: str,
    title: str,
    imdb_id: str,
    season: int | None,
    episode: int | None,
    selection: str,
    cast_meta: caster.CastMeta | None = None,
) -> int:
    """Resolve the best stream for one video and play/cast it headlessly, then emit JSON.
    Reuses the same primitives as the interactive flow (api.streams → prepare_stream →
    auto_subs → play/cast) but never opens fzf (auto=True, reselect_on_wrong_audio=False)
    and never silently falls back to local when a requested cast device is missing."""
    print(f"▶ {title} — cerco la sorgente migliore…", file=sys.stderr)
    results = api.streams(cfg, typ, video_id)
    if not results:
        _emit_json(
            {
                "ok": False,
                "error": "no_streams",
                "message": stream_select.no_streams_message(cfg, typ, video_id, title),
            }
        )
        return 1
    available_audio = stream_select.audio_languages(cfg, results, cast=opts.cast)

    audio_verified: bool | None = None
    if opts.audio_lang:
        # Forced dub: explicit error if no stream carries it (no silent fallback).
        if opts.audio_lang not in available_audio:
            _emit_json(
                {
                    "ok": False,
                    "error": "audio_lang_unavailable",
                    "message": f"audio «{opts.audio_lang}» non disponibile per «{title}»",
                    "available_audio": list(available_audio),
                }
            )
            return 1
        # Track-accurate: ffprobe-confirm the real tracks carry the dub (the name tag can
        # lie). None back = every name-match's real tracks lack the language.
        chosen, audio_verified = stream_select.pick_audio_stream_verified(
            cfg, results, opts.audio_lang, cast=opts.cast
        )
        if chosen is None:
            _emit_json(
                {
                    "ok": False,
                    "error": "audio_lang_unavailable",
                    "message": f"audio «{opts.audio_lang}» assente dalle tracce reali di «{title}»",
                    "available_audio": list(available_audio),
                }
            )
            return 1
        vetted = stream_select.VettedStream(stream=chosen, auto=True, safety_sub_lang=None)
    else:
        vetted = stream_select.prepare_stream(
            cfg, results, opts, auto=True, reselect_on_wrong_audio=False
        )
    if vetted is None:
        _emit_json(
            {
                "ok": False,
                "error": "no_playable_stream",
                "message": f"nessuno stream riproducibile per «{title}»",
            }
        )
        return 1
    chosen = vetted.stream
    stream_block = _stream_block(cfg, chosen)

    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    device_name: str | None = None
    volume: float | None = None
    muted: bool | None = None
    notice: str | None = None
    reencoded = False  # set when a Tier-2 audio remux was used for the cast
    # Reported audio language/subtitles: defaults for the local path, overridden by the cast
    # language decision (`vet_cast_audio`) so the JSON reflects what actually plays.
    cast_audio_lang = opts.audio_lang or (cfg.primary or None)
    cast_audio_verified = audio_verified
    cast_sub_lang = vetted.safety_sub_lang or opts.sub_lang
    with tempfile.TemporaryDirectory(prefix="nstream-", dir=runtime) as work_dir:
        start = _resume_position(cfg, video_id) if opts.history else None
        sub_paths = auto_subs(
            cfg, typ, video_id, work_dir, opts, safety_sub_lang=vetted.safety_sub_lang
        )
        if opts.cast:
            try:
                device = _resolve_device(cfg, headless=True, prefer=args.device)
            except CastUnavailable as e:
                _emit_json({"ok": False, "error": "device_not_found", "message": str(e)})
                return 1
            device_name = args.device or cfg.cast_device or device
            # Enforce the primary audio language for the cast: the DMR plays a file's first
            # track and can't switch, so decide direct vs remux-to-select-the-track (or reselect
            # a dub that has it) up front. Default fire-and-return unless --follow.
            target_lang = opts.audio_lang or cfg.primary
            plan = stream_select.vet_cast_audio(cfg, results, chosen, target_lang)
            chosen = plan.stream
            stream_block = _stream_block(cfg, chosen)  # may have been reselected
            if plan.real_lang:
                cast_audio_lang, cast_audio_verified = plan.real_lang, plan.verified
            if plan.mode == "absent" and target_lang:
                # No dub carries the primary language: cast the best pick with primary subs.
                cast_sub_lang = target_lang
                sub_paths = auto_subs(
                    cfg, typ, video_id, work_dir, opts, safety_sub_lang=target_lang
                )
            cm = cast_meta or caster.CastMeta()

            def on_cast_event(ev: dict) -> None:
                """Emit one JSONL line per castbridge event for `--json --cast --follow`."""
                kind = ev.get("kind")
                _emit_json(
                    {
                        "ok": kind != "failed",
                        "action": "cast",
                        "event": kind,
                        **{k: v for k, v in ev.items() if k != "kind"},
                    }
                )

            follow_cb = on_cast_event if args.follow else None
            # Backend strategy (see _play_on_cast): mirror only when the audio is undecodable
            # by the DMR (plan.mode == "remux") and --mirror is set — there it beats the remux
            # prepare-wait. Decodable audio always goes DMR-direct (instant + native).
            if opts.mirror and mirror.available() and plan.mode == "remux":
                mirror.cast_via_mirror(
                    cfg, title, chosen["url"],
                    device=device, start=start, sub_paths=sub_paths, follow=bool(args.follow),
                )  # fmt: skip
                action = "mirror"
            else:
                if opts.mirror and plan.mode != "remux":
                    print(
                        "nstream: audio decodificabile dal TV → cast diretto "
                        "(mirror non necessario)",
                        file=sys.stderr,
                    )
                remux_path = (
                    remux.remux_for_cast(
                        chosen["url"],
                        cfg,
                        audio_index=plan.audio_index,
                        size_gb=quality.parse_stream(chosen).size_gb,
                    )  # fmt: skip
                    if plan.mode == "remux"
                    else None
                )
                if remux_path:
                    remux.cast_file(
                        cfg, title, remux_path,
                        device=device, start=start, sub_paths=sub_paths, follow=bool(args.follow),
                        meta=cm, on_event=follow_cb,
                    )  # fmt: skip
                    reencoded = True
                else:
                    cast(
                        cfg, title, chosen["url"],
                        device=device, start=start, sub_paths=sub_paths,
                        langs=(), resolve_lang=None, follow=bool(args.follow),
                        meta=cm, on_event=follow_cb,
                    )  # fmt: skip
                action = "cast"
                # Optional explicit volume (closes the loop with the zero-volume detection).
                if args.volume is not None:
                    caster.set_volume(device, args.volume)
                # Fire-and-return skips the poll loop's volume guard — read it once so a muted
                # or zero-volume receiver (a silent cast that looks fine) is surfaced.
                volume, muted = device_volume(device)
                if muted or volume == 0:
                    notice = "volume del Chromecast a 0 — alza col telecomando o 'catt volume N'"
                    print(f"nstream: {notice}", file=sys.stderr)
        else:
            # Local mpv blocks until the window closes (intended; the user is watching).
            play(
                cfg, title, chosen["url"],
                start=start, sub_paths=sub_paths, cast_enabled=False, work_dir=work_dir,
            )  # fmt: skip
            action = "play"

    _emit_json(
        {
            "ok": True,
            "action": action,
            "title": title,
            "type": typ,
            "imdb_id": imdb_id,
            "season": season,
            "episode": episode,
            "selection": selection,
            "stream": stream_block,
            "reencoded": reencoded,
            "device": device_name,
            "volume": volume,
            "muted": muted,
            "audio_lang": cast_audio_lang,
            "audio_verified": cast_audio_verified,
            "available_audio": list(available_audio),
            "subtitles": cast_sub_lang if sub_paths else None,
            "notice": notice,
            "error": None,
        }
    )
    return 0


def _run_auto_resume(cfg: Config, args: argparse.Namespace, opts: PlayOpts, query: str) -> int:
    """Headless resume (`--json -c`): pick a history entry by normalized title (or the most
    recent when no query) and replay it, with no fzf."""
    entries = state.recent(cfg)
    if not entries:
        _emit_json({"ok": False, "error": "no_result", "message": "cronologia vuota"})
        return 1
    entry = entries[0]
    if query:
        q = _norm_title(query)
        entry = next((e for e in entries if _norm_title(e.get("title", "")) == q), None)
        if entry is None:
            _emit_json(
                {
                    "ok": False,
                    "error": "no_result",
                    "message": f"nessuna cronologia per «{query}»",
                }
            )
            return 1
    typ = entry.get("type", "movie")
    return _auto_play(
        cfg, args, opts, typ, entry["video_id"],
        display_title(entry.get("title", "?"), _entry_video(entry)),
        entry.get("series_id") or entry["video_id"],
        entry.get("season") or None, entry.get("episode") or None, "resume",
    )  # fmt: skip


def _headless_device(cfg: Config, args: argparse.Namespace) -> str | None:
    """Resolve the cast device for a lifecycle action (--stop/--status), or None (emitting a
    device_not_found JSON) when no Chromecast can be resolved without a picker."""
    try:
        return _resolve_device(cfg, headless=True, prefer=args.device)
    except CastUnavailable as e:
        _emit_json({"ok": False, "error": "device_not_found", "message": str(e)})
        return None


def _run_stop(cfg: Config, args: argparse.Namespace) -> int:
    """`--json --stop`: stop the cast on the resolved device."""
    # A mirror cast is self-contained (sender + headless mpv + null sink, no DMR session):
    # tear it down first, independent of DMR device resolution.
    mirror_stopped = mirror.stop()
    device = _headless_device(cfg, args)
    if device is None:
        if mirror_stopped:
            _emit_json({"ok": True, "action": "stop", "device": None, "error": None})
            return 0
        return 1
    ok = caster.stop(device)
    # A castbridge cast lives in the daemon (not in catt), so stop that session too.
    if bridge.bridge_available():
        ok = bridge.stop(device) or ok
    # Also tear down a detached Tier-2 remux server + its temp file, if one is serving.
    remux.stop(device)
    ok = ok or mirror_stopped
    _emit_json(
        {"ok": ok, "action": "stop", "device": device, "error": None if ok else "stop_failed"}
    )
    return 0 if ok else 1


def _run_status(cfg: Config, args: argparse.Namespace) -> int:
    """`--json --status`: report the receiver's current playback state."""
    device = _headless_device(cfg, args)
    if device is None:
        return 1
    st = caster.status(device)
    _emit_json({"ok": True, "action": "status", "device": device, **st, "error": None})
    return 0


def run_continue(cfg: Config, opts: PlayOpts) -> int:
    """`-c`: resume from history, returning to the list after each play (ESC exits)."""
    entries = state.recent(cfg)
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
        entries = state.recent(cfg)  # reflect updated positions, then re-show


# Home-menu action kinds (the value half of an fzf item; history entries are dicts).
_SEARCH = "search"
_BROWSE = "browse"
_SETTINGS = "settings"


def run_home(cfg: Config, opts: PlayOpts) -> int:
    """The TUI home: continue-watching + search + browse + settings, in one menu.
    Loops until the user backs out (ESC). This is the rich entry surface — the
    desktop/fuzzel launcher only opens it; no UI logic lives in fuzzel."""
    notice: str | None = None
    while True:
        recent = state.recent(cfg) if opts.history else []
        g = ui.glyphs(ui.active_caps())
        items: list[tuple[str, object]] = [(history_label(e), e) for e in recent]
        items += [
            (f"{g.search}  Cerca…", (_SEARCH, "")),
            (f"{g.fire}  Popolari", (_BROWSE, "popolari")),
            (f"{g.new}  Novità", (_BROWSE, "nuovi")),
            (f"{g.star}  Top IMDb", (_BROWSE, "top")),
            (f"{g.gear}  Impostazioni", (_SETTINGS, "")),
        ]

        def home_preview(value: object) -> str | None:
            # Action rows (tuples) have no preview; continue-watching entries (dicts) do.
            return (
                None
                if isinstance(value, tuple)
                else _entry_preview(typecast("HistoryEntry", value))
            )

        # The Tab hint only applies to the continue-watching rows.
        header = notice or (_pick_hint(opts) if recent else None)
        chosen = fzf_key(items, "nstream> ", header=header, preview=home_preview)
        notice = None
        if chosen is None:
            return 0
        key, value = chosen
        if not isinstance(value, tuple):  # a continue-watching entry
            notice = play_history(cfg, typecast("HistoryEntry", value), _apply_key(opts, key))
            continue
        kind, value = value
        if kind == _SEARCH:
            try:
                query = input("cerca> ").strip()
            except EOFError:
                return 0
            if query:
                run_search(cfg, query, opts)
        elif kind == _BROWSE:
            run_browse(cfg, CAT_MAP[typecast(str, value)], opts)
        elif kind == _SETTINGS:
            settings.run_settings(cfg)
            cfg = load()  # pick up any change for the next loop


def _dispatch(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    if args.json:
        # Headless: no _clear, no fzf, JSON on stdout. Catch NetworkError here so a network
        # failure emits a JSON error object, not the human stderr message main() would print
        # (which would break the agent parsing stdout).
        try:
            return run_auto(cfg, args, opts)
        except api.NetworkError as e:
            _emit_json({"ok": False, "error": "network", "message": str(e)})
            return 1
    _clear()  # start the interactive session on a clean screen (drop launcher banner)
    if args.cont:
        return run_continue(cfg, opts)
    if args.browse:
        return run_browse(cfg, CAT_MAP[args.browse], opts)
    query = " ".join(args.query)
    if args.explain:
        if not query:
            print("nstream: --explain richiede un titolo da cercare", file=sys.stderr)
            return 2
        return run_explain(cfg, query)
    if query:
        return run_search(cfg, query, opts)
    return run_home(cfg, opts)


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
    mirror = (args.mirror or cfg.cast_mode == "mirror") and not args.local
    opts = PlayOpts(
        # --json is headless: always auto-pick (no fzf stream menu).
        auto=cfg.auto_play or args.play or args.json,
        cast=(cfg.prefer_cast or args.cast or mirror) and not args.local,
        sub_mode=sub_mode,
        sub_lang=sub_lang,
        history=cfg.history_enabled and not args.no_history,
        autoplay=cfg.autoplay and not args.no_autoplay,
        audio_lang=args.audio_lang or None,
        mirror=mirror,
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
