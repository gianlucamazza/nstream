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

from . import api, log, oshash, srt, subalign, tracks, ui
from .config import Config, PlayOpts, Stream, Subtitle
from .labels import audio_summary, sub_summary, track_label
from .picker import fzf

_log = log.get_logger("subs")


@dataclass(frozen=True)
class SubsPick:
    """Outcome of the no-menu subtitle acquisition: the downloaded file(s) plus HOW the
    track was chosen — `"hash"` (protocol-verified OSHash match), `"audio"` (offset
    measured against the media's real audio, ADR 0020) or `"lang"` (best language
    guess, sync not guaranteed). Reported in the headless JSON (`subtitles_match` +
    `subtitles_offset`) so a guess is never presented as a match. `alternates` carries
    the undelivered same-language candidates: the local-media alignment tier can fall
    back to them when the delivered track refuses to align."""

    paths: tuple[str, ...] = ()
    match: str | None = None  # "hash" | "audio" | "lang" when paths is non-empty
    offset_s: float | None = None  # measured+applied correction when match == "audio"
    alternates: tuple[Subtitle, ...] = ()


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
    # auto: hash-match (protocol-verified) → honest language guess; the audio tier
    # (ADR 0020) runs later, against the local media file, in align_local().
    pick = _choose(subs, pref, work_dir)
    if pick is None:
        print("nstream: nessun sottotitolo nelle lingue preferite", file=sys.stderr)
        return SubsPick()
    return pick


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in ("127.0.0.1", "::1", "localhost")


# Same-language candidates kept for the local-alignment fallback (downloads are KB).
_POOL_CAP = 6


def _choose(subs: list[Subtitle], pref: dict[str, int], work_dir: str) -> SubsPick | None:
    """Evidence-tier selection over the (lang, hash)-sorted candidates (ADR 0020).

    Tier 1 — protocol hash match (`m == "h"`): synced by construction, wins outright.
    Tier 3 — honest language guess: first candidate by rank, `match="lang"`; the
    undelivered pool rides along as `alternates` for tier 2, the LOCAL-media audio
    alignment, which runs later (cast_flow) because the full media file only exists
    after a Tier-2 remux. The runtime-fit heuristic that used to sit between them was
    field-falsified (end-cue adherence picked a track 14 s late) and is gone.
    None when no candidate is in a preferred language."""
    best_lang = subs[0].get("lang", "")
    if best_lang not in pref:
        return None
    pool = [s for s in subs if s.get("lang", "") == best_lang][:_POOL_CAP]
    if pool[0].get("hash_match"):  # sorted hash-first within the language
        path = _download_subtitle(pool[0], work_dir)
        if not path:
            return SubsPick()
        print("nstream: sottotitoli sincronizzati al file (hash-match)", file=sys.stderr)
        return SubsPick((path,), "hash")
    path = _download_subtitle(pool[0], work_dir)
    if not path:
        return SubsPick()
    return SubsPick((path,), "lang", alternates=tuple(pool[1:]))


retime_srt = srt.retime
to_vtt = srt.to_vtt


def align_local(
    cfg: Config, pick: SubsPick, media_path: str, work_dir: str, opts: PlayOpts
) -> SubsPick:
    """Tier 2 of the selection pipeline (ADR 0020): audio-anchored verification and
    correction of the delivered subtitle against a LOCAL media file — the Tier-2 remux
    output, the very file the receiver will play. Full-signal alignment is the regime
    where interval alignment is proven; the sparse remote mode was measured unviable
    (100-300 MB/cast) and lives only in the Phase-0 bench.

    Measure-then-apply: the engine never touches files; the intact original is retimed
    here. Refusals leave the pick unchanged (honest `"lang"`) with the reason logged.
    A refusing delivered track falls back to the alternate candidates: a garbage SRT
    refuses, the next family may align. Manual `--sub-offset`/`--sub-fps` wins (the
    engine steps aside), as does a protocol hash match."""
    manual_retime = bool(opts.sub_offset) or opts.sub_scale != 1.0
    if not pick.paths or pick.match != "lang" or manual_retime:
        return pick
    if not cfg.sub_align or not subalign.available():
        return pick
    duration = tracks.probe_tracks(media_path).duration
    fp = subalign.probe_local(media_path, duration=duration)
    if isinstance(fp, str):
        _log.info("align_local: fingerprint rifiutato (%s)", fp)
        return pick
    candidates: list[tuple[str, Subtitle | None]] = [(pick.paths[0], None)]
    for alt in pick.alternates[:3]:
        candidates.append(("", alt))
    for path, alt in candidates:
        if not path:
            if alt is None:
                continue
            path = _download_subtitle(alt, work_dir) or ""
            if not path:
                continue
        verdict = subalign.align(srt.cue_spans(path), fp)
        d = verdict.diag
        if verdict.reason != "aligned" or verdict.offset_s is None:
            _log.info(
                "align_local: candidato rifiutato (%s%s)",
                verdict.reason,
                f", score {d.score:.2f}" if d else "",
            )
            continue
        offset = verdict.offset_s
        if abs(offset) >= 0.5 and not srt.retime(path, offset, 1.0):
            continue
        print(
            f"nstream: sottotitoli allineati all'audio del file (offset {offset:+.1f}s)",
            file=sys.stderr,
        )
        return SubsPick((path,), "audio", offset_s=offset)
    return pick


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
