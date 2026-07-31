"""Headless play/cast of one resolved title (`--json` body after meta selection).

Owns stream fetch → prepare_stream → local play / cast_flow → success JSON.
Lifecycle actions (--stop/--status/…) and title selection stay in `headless`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import replace

from . import (
    api,
    cast_flow,
    caster,
    quality,
    state,
    stream_select,
    ui,
)
from .caster import CastUnavailable, device_volume
from .caster import resolve_device as _resolve_device
from .config import Config, PlayOpts
from .player import play
from .subs import auto_subs
from .subs import stream_filename as subs_filename
from .types import HistoryEntry, Stream


def emit_json(obj: dict) -> None:
    """One machine-readable JSON object on stdout (no url/token)."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def describe_stream(cfg: Config, chosen: Stream) -> dict:
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


def auto_play(
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
    *,
    name: str | None = None,
) -> int:
    """Resolve the best stream for one video and play/cast it headlessly, then emit JSON.
    Reuses the same primitives as the interactive flow (api.streams → prepare_stream →
    auto_subs → play/cast) but never opens fzf (auto=True, reselect_on_wrong_audio=False)
    and never silently falls back to local when a requested cast device is missing."""
    print(f"{ui.g().play} {title} — cerco la sorgente migliore…", file=sys.stderr)
    results = api.streams(cfg, typ, video_id)
    if not results:
        err = stream_select.no_stream_source_error(cfg) or "no_streams"
        emit_json(
            {
                "ok": False,
                "error": err,
                "message": stream_select.no_streams_message(cfg, typ, video_id, title),
            }
        )
        return 1
    # Sources proven removed from the debrid (ADR 0025) are dropped before anything is
    # measured, so `available_audio`/`available_resolutions` describe what can actually play.
    results, removed = stream_select.prune_dead(cfg, results)
    if not results:
        emit_json(
            {
                "ok": False,
                "error": "sources_removed",
                "message": f"tutte le sorgenti note per «{title}» risultano rimosse dal debrid",
                "removed_sources": removed,
            }
        )
        return 1

    available_audio = stream_select.audio_languages(cfg, results, cast=opts.cast)
    available_resolutions = stream_select.available_resolutions(cfg, results, cast=opts.cast)
    exact = stream_select.exact_resolution(opts.quality)

    # Hard quality filter: fail fast with the available tiers (like audio_lang_unavailable).
    if exact and exact not in available_resolutions:
        emit_json(
            {
                "ok": False,
                "error": "quality_unavailable",
                "message": f"nessuno stream {exact}p per «{title}»",
                "available_resolutions": available_resolutions,
            }
        )
        return 1

    audio_verified: bool | None = None
    keys_before: list[str] = []  # set on the prepare_stream path (see `sources_removed` below)
    if opts.audio_lang:
        # Forced dub: explicit error if no stream carries it (no silent fallback).
        if opts.audio_lang not in available_audio:
            emit_json(
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
            cfg, results, opts.audio_lang, cast=opts.cast, exact_resolution=exact
        )
        if chosen is None:
            emit_json(
                {
                    "ok": False,
                    "error": "audio_lang_unavailable",
                    "message": f"audio «{opts.audio_lang}» assente dalle tracce reali di «{title}»",
                    "available_audio": list(available_audio),
                }
            )
            return 1
        vetted = stream_select.VettedStream(
            stream=chosen, auto=True, safety_sub_lang=None, quality=opts.quality or 0
        )
    else:
        # Keys, not just a count: after the verification runs, `sources_removed` must rest on
        # what was actually proven gone (ADR 0025), never on "the list came back empty" — a
        # source merely unusable right now is a different answer for the caller.
        keys_before = [stream_select.source_key(s) for s in results]
        vetted = stream_select.prepare_stream(
            cfg, results, opts, auto=True, reselect_on_wrong_audio=False, title=title
        )
    if vetted is None:
        # The pre-commit verification may have just proven the remaining sources gone
        # (ADR 0025): report that only when the denylist says so, so the caller learns
        # "removed" instead of a generic "nothing playable" that invites a pointless retry.
        proven_gone = sum(1 for k in keys_before if state.is_dead(k))
        if proven_gone and proven_gone == len(keys_before):
            emit_json(
                {
                    "ok": False,
                    "error": "sources_removed",
                    "message": f"le sorgenti di «{title}» risultano rimosse dal debrid",
                    "removed_sources": removed + proven_gone,
                }
            )
            return 1
        # Quality filter may have emptied the set even if the pre-check passed (e.g. HW).
        if exact:
            emit_json(
                {
                    "ok": False,
                    "error": "quality_unavailable",
                    "message": f"nessuno stream {exact}p riproducibile per «{title}»",
                    "available_resolutions": available_resolutions,
                }
            )
            return 1
        emit_json(
            {
                "ok": False,
                "error": "no_playable_stream",
                "message": f"nessuno stream riproducibile per «{title}»",
            }
        )
        return 1
    chosen = vetted.stream
    stream_block = describe_stream(cfg, chosen)
    # ADR 0021: resolve the per-invocation quality into opts — the cast decision tree
    # threads it through every reselect path.
    opts = replace(opts, quality=vetted.quality)

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
    sub_match: str | None = None  # "hash" | "audio" | "lang" — how the track was chosen
    sub_offset: float | None = None  # applied correction (s) when sub_match == "audio"
    subs_delivered = True  # local mpv always renders requested subs; cast paths override
    # History bookkeeping: the plain show name (the decorated `title` would break the
    # `-c <titolo>` normalized-title match), and the end position when a path can know it.
    show_name = name or title
    hist_pos = hist_dur = 0.0

    def _hist_entry(pos: float, dur: float) -> HistoryEntry:
        return state.make_entry(
            video_id, show_name, typ, pos, dur,
            series_id=imdb_id if typ == "series" else "",
            season=season or 0, episode=episode or 0,
        )  # fmt: skip

    with tempfile.TemporaryDirectory(prefix="nstream-", dir=runtime) as work_dir:
        start = state.resume_position(cfg, video_id) if opts.history else None
        if opts.cast:
            try:
                device = _resolve_device(cfg, headless=True, prefer=args.device)
            except CastUnavailable as e:
                emit_json({"ok": False, "error": "device_not_found", "message": str(e)})
                return 1
            device_name = args.device or cfg.cast_device or device

            def on_cast_event(ev: dict) -> None:
                """Emit one JSONL line per castbridge event for `--json --cast --follow`."""
                kind = ev.get("kind")
                emit_json(
                    {
                        "ok": kind != "failed",
                        "action": "cast",
                        "event": kind,
                        **{k: v for k, v in ev.items() if k != "kind"},
                    }
                )

            # Shared decision tree (see cast_flow.run_cast): vet the video codec and the
            # audio plan, then mirror / Tier-2 remux / direct. Default fire-and-return
            # unless --follow; no in-cast switch (headless has no 'a' key) →
            # allow_lang_switch stays False.
            try:
                outcome = cast_flow.run_cast(
                    cfg, results, chosen,
                    device=device, title=title, typ=typ, video_id=video_id, work_dir=work_dir,
                    opts=opts, start=start, follow=bool(args.follow),
                    meta=cast_meta or caster.CastMeta(),
                    on_event=on_cast_event if args.follow else None,
                    safety_sub_lang=vetted.safety_sub_lang,
                )  # fmt: skip
            except cast_flow.CastVideoUnsupported as e:
                emit_json(
                    {
                        "ok": False,
                        "error": "video_codec_unsupported",
                        "message": f"video {e.codec} non decodificabile dal Chromecast e "
                        "nessuna release alternativa castabile; riprova con --local "
                        "o con un'altra qualità (--quality)",
                        "video_codec": e.codec,
                    }
                )
                return 1
            chosen = outcome.stream
            stream_block = describe_stream(cfg, chosen)  # may have been reselected
            if outcome.audio_lang:
                cast_audio_lang, cast_audio_verified = outcome.audio_lang, outcome.audio_verified
            cast_sub_lang = outcome.safety_sub_lang or opts.sub_lang
            sub_paths = outcome.sub_paths
            sub_match = outcome.sub_match
            sub_offset = outcome.sub_offset
            subs_delivered = outcome.subs_delivered
            action, reencoded, notice = outcome.action, outcome.reencoded, outcome.notice
            if args.follow:
                hist_pos, hist_dur = outcome.pos, outcome.dur
            elif opts.history:
                # Fire-and-return: no poll loop ever sees the end position, so at least
                # record that this title/episode started (a later `-c` proposes it instead
                # of restarting the series at S01E01); `--stop`/`--status` merge the real
                # receiver position into it via the cast session.
                started = _hist_entry(float(start or 0.0), 0.0)
                state.note_started(cfg, started)
                if action == "cast":  # mirror has no DMR media session to read back
                    # Session key = the resolved IP (`device`), NOT `device_name`: --stop
                    # and --status compare against resolve_device()'s IP, so a configured
                    # name ("Salotto") would never match and the merge would be inert.
                    state.remember_cast(cfg, started, device)
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
            subs_pick = auto_subs(
                cfg, typ, video_id, work_dir, opts, safety_sub_lang=vetted.safety_sub_lang,
                video_url=chosen.get("url"), filename=subs_filename(chosen),
            )  # fmt: skip
            sub_paths, sub_match = subs_pick.paths, subs_pick.match
            sub_offset = subs_pick.offset_s
            # Local mpv blocks until the window closes (intended; the user is watching).
            hist_pos, hist_dur, _sig = play(
                cfg, title, chosen["url"],
                start=start, sub_paths=sub_paths, cast_enabled=False, work_dir=work_dir,
            )  # fmt: skip
            action = "play"

    # Same guard as the interactive flow: only persist a resume we can reason about —
    # without a real duration the watched/near-end logic can't ever retire the entry.
    if opts.history and hist_pos > 0 and hist_dur > 0:
        state.save_entry(cfg, _hist_entry(hist_pos, hist_dur))

    emit_json(
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
            # Echo the requested quality (0/null = Auto / no filter); stream.resolution is actual.
            "quality": vetted.quality if vetted.quality else None,
            "available_resolutions": available_resolutions,
            "reencoded": reencoded,
            "device": device_name,
            "volume": volume,
            "muted": muted,
            "audio_lang": cast_audio_lang,
            "audio_verified": cast_audio_verified,
            "available_audio": list(available_audio),
            # Only claim subtitles the delivery actually attached (`subs_delivered`): both the
            # castbridge (side-loaded WebVTT track) and catt (`-s`) paths carry them now, but a
            # mirror cast or a failed conversion may not — don't report those as active.
            "subtitles": cast_sub_lang if (sub_paths and subs_delivered) else None,
            # How the track was chosen (ADR 0020): "hash" = protocol-verified OSHash
            # match, "audio" = aligned against the local media's real audio (offset in
            # subtitles_offset), "lang" = best language guess (correct with --sub-offset).
            "subtitles_match": sub_match if (sub_paths and subs_delivered) else None,
            "subtitles_offset": sub_offset if (sub_paths and subs_delivered) else None,
            "notice": notice,
            "error": None,
        }
    )
    return 0
