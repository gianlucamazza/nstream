"""Subtitle acquisition + the pre-play audio/subtitle track menu.

Fetch OpenSubtitles tracks, rank by preferred language, download (gunzip) into the
per-play temp dir. Also owns `choose_tracks` (the fzf menu over ffprobe tracks +
OpenSubtitles) so the orchestrator stays free of leaf-menu code."""

from __future__ import annotations

import contextlib
import gzip
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import cast as typecast

from . import api, oshash, srt, subsync, tracks, ui
from .config import Config, PlayOpts, Stream, Subtitle
from .labels import audio_summary, sub_summary, track_label
from .picker import fzf


@dataclass(frozen=True)
class SubsPick:
    """Outcome of the no-menu subtitle acquisition: the downloaded file(s) plus HOW the
    track was chosen — `"hash"` (protocol-verified OSHash match), `"audio"` (offset
    measured on the media's real audio, ADR 0019), `"runtime"` (last cue fits the media
    duration) or `"lang"` (best language guess, sync not guaranteed). Reported in the
    headless JSON (`subtitles_match`) so a guess is never presented as a match."""

    paths: tuple[str, ...] = ()
    match: str | None = None  # "hash" | "lang" when paths is non-empty


def stream_filename(stream: Stream) -> str | None:
    """The release filename Torrentio exposes in behaviorHints — the `filename` extra of
    a subtitles hash query (improves matching per the OpenSubtitles guidance)."""
    hints = stream.get("behaviorHints")
    name = hints.get("filename") if isinstance(hints, dict) else None
    return name if isinstance(name, str) and name else None


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
                got = pick_subtitles(cfg, typ, video_id, work_dir, mode="menu", video_url=url)
                if got:
                    sub_paths, sid = got, None
            else:
                sid, sub_paths = typecast("str | int", pick), ()


def _download_subtitle(sub: Subtitle, work_dir: str) -> str | None:
    url = sub.get("url")
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": api.UA})
        with urllib.request.urlopen(req, timeout=api.TIMEOUT) as resp:
            raw = resp.read()
    except OSError:
        print("nstream: download sottotitolo fallito", file=sys.stderr)
        return None
    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        with contextlib.suppress(OSError):
            raw = gzip.decompress(raw)
    # Written into the per-play temp dir so it is cleaned up with everything else.
    # `lang` comes from the OpenSubtitles response (external data): keep only alnum
    # chars so a hostile value can't inject path separators / traversal into the prefix.
    lang = "".join(c for c in sub.get("lang", "") if c.isalnum()) or "sub"
    fd, path = tempfile.mkstemp(prefix=f"{lang}-", suffix=".srt", dir=work_dir)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path


def available_subtitle_langs(cfg: Config, typ: str, video_id: str) -> list[str]:
    """Sorted list of subtitle languages available for a video (best-effort: [] on a
    network error). Used by the headless `--probe` discovery — fetches, never downloads."""
    try:
        subs = api.subtitles(cfg, typ, video_id)
    except api.NetworkError:
        return []
    return sorted({s.get("lang", "") for s in subs if s.get("lang")})


def pick_subtitles(
    cfg: Config,
    typ: str,
    video_id: str,
    work_dir: str,
    *,
    mode: str = "auto",
    lang: str | None = None,
    video_url: str | None = None,
) -> tuple[str, ...]:
    """Back-compat façade over `_pick` for callers that only need the file paths
    (the interactive menu). The no-menu paths use `auto_subs` → SubsPick."""
    return _pick(cfg, typ, video_id, work_dir, mode=mode, lang=lang, video_url=video_url).paths


def _pick(
    cfg: Config,
    typ: str,
    video_id: str,
    work_dir: str,
    *,
    mode: str = "auto",
    lang: str | None = None,
    video_url: str | None = None,
    filename: str | None = None,
) -> SubsPick:
    """Fetch, rank and download one subtitle track. With `video_url` the stream's OSHash
    is computed first (two 64 KB ranged reads) and hash-matched tracks — timed for the
    exact file — win within a language (ADR 0018). Language stays the primary key: a
    perfectly synced track in the wrong language helps nobody."""
    video_hash: str | None = None
    video_size = 0
    # Loopback = the local P2P gateway (TorrServer): a tail Range read there forces the
    # torrent's LAST piece at startup, the exact anti-pattern piece-deadline scheduling
    # avoids. Hash only direct (debrid/CDN) urls, where a ranged read is cheap.
    if video_url and not _is_loopback(video_url) and (hashed := oshash.hash_url(video_url)):
        video_hash, video_size = hashed
    try:
        subs = api.subtitles(
            cfg, typ, video_id,
            video_hash=video_hash, video_size=video_size, filename=filename,
        )  # fmt: skip
    except api.NetworkError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return SubsPick()
    if not subs:
        print("nstream: nessun sottotitolo", file=sys.stderr)
        return SubsPick()
    langs = [lang] if lang else cfg.subtitle_langs
    pref = {code: i for i, code in enumerate(langs)}
    subs.sort(key=lambda s: (pref.get(s.get("lang", ""), len(pref)), not s.get("hash_match")))
    if mode == "menu":
        items = [
            (
                f"{s.get('lang', '?'):5s} {s.get('id', '')}"
                + ("  · sync verificata (hash)" if s.get("hash_match") else ""),
                s,
            )
            for s in subs
        ]
        chosen = fzf(items, "sottotitoli> ")
        if not chosen:
            return SubsPick()
        path = _download_subtitle(chosen, work_dir)
        if not path:
            return SubsPick()
        return SubsPick((path,), "hash" if chosen.get("hash_match") else "lang")
    # auto: hash-match (protocol-verified) → runtime fit → best language guess.
    pick = _auto_choose(subs, pref, video_url, work_dir)
    if pick is None:
        print("nstream: nessun sottotitolo nelle lingue preferite", file=sys.stderr)
        return SubsPick()
    return pick


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in ("127.0.0.1", "::1", "localhost")


# Text-format concerns moved to the `srt` leaf module (shared with the alignment
# engine and the delivery tier). Thin aliases keep the existing call sites and the
# public test surface stable; `_decode_sub` dies with the runtime-fit in ADR 0020.
_decode_sub = srt.decode
_SRT_TS = srt._TS


# Runtime-fit knobs (ADR 0018 refinement): candidates checked per language, and the
# last-cue-vs-duration gap past which the fit is not trusted (reported as a plain guess).
_FIT_CAP = 6
_FIT_WARN_S = 120.0


def _last_cue_s(path: str) -> float | None:
    """Last cue timestamp (seconds) of an SRT, or None when unparsable."""
    text = _decode_sub(path)
    if not text:
        return None
    times = [
        int(h) * 3600 + int(m) * 60 + int(sec) + int(ms.ljust(3, "0")) / 1000
        for ln in text.splitlines()
        if "-->" in ln
        for h, m, sec, ms in _SRT_TS.findall(ln)
    ]
    return max(times) if times else None


def _auto_choose(
    subs: list[Subtitle], pref: dict[str, int], video_url: str | None, work_dir: str
) -> SubsPick | None:
    """Auto-mode selection over the (lang, hash)-sorted candidates. A REAL hash match
    (protocol `m == "h"`) is synced by construction and wins. Otherwise, with several
    same-language candidates and a known media duration, pick by RUNTIME FIT: the track
    whose last cue lands closest to the file's real duration — measurable sync evidence
    (live case: two timing clusters 80 s apart; the addon's arbitrary order picked the
    wrong one). Downloads are a few KB gzipped each, capped at `_FIT_CAP`. None when no
    candidate is in a preferred language."""
    best_lang = subs[0].get("lang", "")
    if best_lang not in pref:
        return None
    pool = [s for s in subs if s.get("lang", "") == best_lang][:_FIT_CAP]
    if pool[0].get("hash_match"):  # sorted hash-first within the language
        path = _download_subtitle(pool[0], work_dir)
        if not path:
            return SubsPick()
        print("nstream: sottotitoli sincronizzati al file (hash-match)", file=sys.stderr)
        return SubsPick((path,), "hash")
    if len(pool) > 1 and video_url:
        duration = tracks.probe_tracks(video_url).duration  # memoized: probed at vetting
        if duration > 600:
            scored: list[tuple[float, str]] = []
            for s in pool:
                p = _download_subtitle(s, work_dir)
                if p and (last := _last_cue_s(p)):
                    scored.append((abs(last - duration), p))
            if scored:
                gap, path = min(scored)
                if gap > _FIT_WARN_S:
                    print(
                        f"nstream: nessun sottotitolo combacia col runtime "
                        f"(scarto minimo {gap:.0f}s) — sync non garantita",
                        file=sys.stderr,
                    )
                    return SubsPick((path,), "lang")
                print(
                    f"nstream: sottotitoli scelti per aderenza al runtime (scarto {gap:.0f}s)",
                    file=sys.stderr,
                )
                return SubsPick((path,), "runtime")
    path = _download_subtitle(pool[0], work_dir)
    return SubsPick((path,), "lang") if path else SubsPick()


retime_srt = srt.retime
to_vtt = srt.to_vtt


def auto_subs(
    cfg: Config, typ: str, video_id: str, work_dir: str, opts: PlayOpts,
    *, safety_sub_lang: str | None = None,
    video_url: str | None = None, filename: str | None = None,
) -> SubsPick:  # fmt: skip
    """Subtitle acquisition for the no-menu paths (cast / --play / binge). When
    `safety_sub_lang` is set (the audio isn't in the primary language), fetch that
    language's subtitles as a safety net regardless of `--subs`. `video_url`/`filename`
    (the resolved stream) enable the exact-file hash match; a manual `--sub-offset`/
    `--sub-fps` retime is applied to the downloaded file before any delivery (ADR 0018)."""
    if safety_sub_lang:
        pick = _pick(
            cfg, typ, video_id, work_dir,
            mode="auto", lang=safety_sub_lang, video_url=video_url, filename=filename,
        )  # fmt: skip
    elif not opts.sub_mode:
        return SubsPick()
    else:
        pick = _pick(
            cfg, typ, video_id, work_dir,
            mode=opts.sub_mode, lang=opts.sub_lang, video_url=video_url, filename=filename,
        )  # fmt: skip
    # ADR 0019: without a protocol hash match, let the media's own audio arbitrate the
    # sync (the runtime-fit was falsified live: end-cue adherence picked a track 14 s
    # late). An explicit manual retime is a user override → the auto pass steps aside.
    manual_retime = bool(opts.sub_offset) or opts.sub_scale != 1.0
    if (
        pick.paths
        and pick.match != "hash"
        and not manual_retime
        and cfg.sub_autosync
        and video_url
        and subsync.available()
    ):
        offset = subsync.measure_offset(
            pick.paths[0], video_url, work_dir, window_s=cfg.sub_autosync_window_s
        )
        # Measure-then-apply: alass only reports the constant offset (gated for
        # plausibility inside measure_offset); the intact original is retimed HERE, so
        # alass's rewritten file (clamped negatives, fps rescale) never reaches the TV.
        if offset is not None and (abs(offset) < 0.5 or retime_srt(pick.paths[0], offset, 1.0)):
            pick = SubsPick(pick.paths, "audio")
            print(
                f"nstream: sottotitoli allineati all'audio (offset {offset:+.1f}s)",
                file=sys.stderr,
            )
    if pick.paths and (opts.sub_offset or opts.sub_scale != 1.0):
        for p in pick.paths:
            retime_srt(p, opts.sub_offset, opts.sub_scale)
        print(
            f"nstream: sottotitoli ritimati (offset {opts.sub_offset:+.2f}s"
            + (f", scala {opts.sub_scale:.4f}" if opts.sub_scale != 1.0 else "")
            + ")",
            file=sys.stderr,
        )
    return pick
