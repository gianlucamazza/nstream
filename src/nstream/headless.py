"""Headless `--json` subsystem: non-interactive play/cast for agents (no fzf, no TTY).

`run()` is the single seam `cli._dispatch` calls when `--json` is set: it wraps `run_auto`
(title/episode resolution → probe / stop / status / resume → `_auto_play`) and turns a
NetworkError into a JSON error object, so an agent parsing stdout never sees a human
stderr message in its place. Exactly one JSON object is emitted per invocation (plus one
JSONL line per castbridge event with `--follow`); diagnostics stay on stderr, and no
stream/debrid url or token ever reaches stdout.

Same tier as `stream_select`/`cast_flow`: sits below `cli` (never imports it) and reuses
the same primitives as the interactive flow — `api.streams` → `prepare_stream` →
`auto_subs` → `play` / `cast_flow.run_cast` — but never opens fzf (auto=True,
reselect_on_wrong_audio=False) and never silently falls back to local playback when a
requested cast device is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from . import (
    api,
    bridge,
    cast_flow,
    caster,
    mirror,
    quality,
    remux,
    series,
    state,
    stream_select,
    ui,
)
from .api import CAT_MAP
from .caster import CastUnavailable, device_volume
from .caster import resolve_device as _resolve_device
from .config import Config, Meta, PlayOpts, Stream
from .labels import display_title
from .player import play
from .subs import auto_subs, available_subtitle_langs


def typ_filter(args: argparse.Namespace) -> str | None:
    """Explicit content-type filter from --movies/--series, or None (mixed)."""
    if args.movies:
        return "movie"
    if args.series:
        return "series"
    return None


def _emit_json(obj: dict) -> None:
    """Write one machine-readable JSON object to stdout (progress stays on stderr).
    Never carries a stream/debrid url or token — only descriptive metadata."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _norm_title(s: str) -> str:
    """Casefold + strip punctuation for tolerant title matching."""
    return " ".join("".join(c if c.isalnum() else " " for c in s.casefold()).split())


def _select_meta(
    metas: list[Meta], query: str, year: str | None, *, want_series: bool = False
) -> tuple[Meta, str]:
    """Pick a title without fzf: an exact normalized-name match (optionally pinned by
    release year) wins, else the first result. Returns (meta, "exact"|"first").

    `want_series` (an explicit --season/--episode) prefers series results — a same-named
    movie must not shadow the series the caller is clearly asking an episode of."""
    if want_series:
        series = [m for m in metas if m.get("type") == "series"]
        metas = series or metas
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


def run(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    """The `--json` entry `cli._dispatch` calls. Catch NetworkError here so a network
    failure emits a JSON error object, not the human stderr message main() would print
    (which would break the agent parsing stdout)."""
    try:
        return run_auto(cfg, args, opts)
    except api.NetworkError as e:
        _emit_json({"ok": False, "error": "network", "message": str(e)})
        return 1


def run_auto(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    """`--json`: headless, non-interactive play/cast. No fzf, no TTY. Emits one JSON
    object on stdout (diagnostics on stderr). Returns 0 on success, 1 on a recoverable
    failure (no result / no stream / device), 2 on a usage error."""
    if args.sub_menu:
        _emit_json(
            {"ok": False, "error": "usage", "message": "--sub-menu incompatibile con --json"}
        )
        return 2
    tfilter = typ_filter(args)
    if tfilter == "movie" and (args.season is not None or args.episode is not None):
        _emit_json(
            {
                "ok": False,
                "error": "usage",
                "message": "--movies incompatibile con --season/--episode",
            }
        )
        return 2
    # Cast lifecycle actions: no title needed, no playback.
    if args.stop:
        return _run_stop(cfg, args)
    if args.status:
        return _run_status(cfg, args)
    query = " ".join(args.query)
    if args.cont:
        return _run_auto_resume(cfg, args, opts, query, tfilter)

    # Meta source: a catalog (--browse) or a title search.
    if args.browse:
        cat = CAT_MAP[args.browse]
        metas = api.catalog(cfg, tfilter, cat) if tfilter else api.browse(cfg, cat)
        if not metas:
            _emit_json({"ok": False, "error": "no_result", "message": "catalogo vuoto"})
            return 1
        meta, selection = metas[0], "browse"
    else:
        if not query:
            _emit_json({"ok": False, "error": "usage", "message": "--json richiede un titolo"})
            return 2
        metas = api.search(cfg, query)
        # Explicit --movies/--series: drop the other type before the title match, so a
        # same-named movie can never shadow the series the caller asked for (and vice versa).
        if tfilter:
            metas = [m for m in metas if m.get("type", "movie") == tfilter]
        if not metas:
            what = {"movie": "nessun film", "series": "nessuna serie"}.get(
                tfilter or "", "nessun risultato"
            )
            _emit_json({"ok": False, "error": "no_result", "message": f"{what} per «{query}»"})
            return 1
        # The --season/--episode series inference stays only when no explicit flag is given.
        meta, selection = _select_meta(
            metas, query, args.year,
            want_series=tfilter is None
            and (args.season is not None or args.episode is not None),
        )  # fmt: skip
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
    print(f"{ui.g().play} {title} — cerco la sorgente migliore…", file=sys.stderr)
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
        start = state.resume_position(cfg, video_id) if opts.history else None
        if opts.cast:
            try:
                device = _resolve_device(cfg, headless=True, prefer=args.device)
            except CastUnavailable as e:
                _emit_json({"ok": False, "error": "device_not_found", "message": str(e)})
                return 1
            device_name = args.device or cfg.cast_device or device

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

            # Shared decision tree (see cast_flow.run_cast): vet the audio plan, then
            # mirror / Tier-2 remux / direct. Default fire-and-return unless --follow;
            # no in-cast switch (headless has no 'a' key) → allow_lang_switch stays False.
            outcome = cast_flow.run_cast(
                cfg, results, chosen,
                device=device, title=title, typ=typ, video_id=video_id, work_dir=work_dir,
                opts=opts, start=start, follow=bool(args.follow),
                meta=cast_meta or caster.CastMeta(),
                on_event=on_cast_event if args.follow else None,
                safety_sub_lang=vetted.safety_sub_lang,
            )  # fmt: skip
            chosen = outcome.stream
            stream_block = _stream_block(cfg, chosen)  # may have been reselected
            if outcome.audio_lang:
                cast_audio_lang, cast_audio_verified = outcome.audio_lang, outcome.audio_verified
            cast_sub_lang = outcome.safety_sub_lang or opts.sub_lang
            sub_paths = outcome.sub_paths
            action, reencoded, notice = outcome.action, outcome.reencoded, outcome.notice
            if action == "cast":
                # Optional explicit volume (closes the loop with the zero-volume detection).
                if args.volume is not None:
                    caster.set_volume(device, args.volume)
                # Fire-and-return skips the poll loop's volume guard — read it once so a muted
                # or zero-volume receiver (a silent cast that looks fine) is surfaced.
                volume, muted = device_volume(device)
                if muted or volume == 0:
                    vol_notice = (
                        "volume del Chromecast a 0 — alza col telecomando o 'catt volume N'"
                    )
                    notice = f"{notice}; {vol_notice}" if notice else vol_notice
                    print(f"nstream: {vol_notice}", file=sys.stderr)
        else:
            sub_paths = auto_subs(
                cfg, typ, video_id, work_dir, opts, safety_sub_lang=vetted.safety_sub_lang
            )
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


def _run_auto_resume(
    cfg: Config, args: argparse.Namespace, opts: PlayOpts, query: str, typ: str | None = None
) -> int:
    """Headless resume (`--json -c`): pick a history entry by normalized title (or the most
    recent when no query) and replay it, with no fzf. `typ` narrows to one content type."""
    entries = state.recent(cfg, typ=typ)
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
        display_title(entry.get("title", "?"), series.entry_video(entry)),
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
