"""Subtitle acquisition.

Fetch OpenSubtitles tracks, rank by preferred language, download (gunzip) into the
per-play temp dir. Menus are injected (ADR 0037); the pre-play track menu lives in
`menus.choose_tracks`."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import os
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import api, languages, log, notices, oshash, srt, subalign, tracks, util
from .config import Config, PlayOpts
from .types import Stream, Subtitle

# The frontend's single-choice menu `(rows, prompt) -> value | None` (ADR 0037).
Choose = Callable[[list[tuple[str, Any]], str], Any]

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
    lang: str | None = None  # language of the delivered track (labels the cast track)


def report_safety_subs(pick: SubsPick, lang: str | None) -> None:
    """Report the OUTCOME of the safety-net subtitle fetch, never the intent.

    Only the caller of `auto_subs` holds the evidence that a track was really acquired, so
    this is the single place allowed to say it was — shared by the local and cast paths so
    the two can't drift apart. The empty outcomes are already reported by `auto_subs`
    itself, so nothing is printed for them here.
    """
    if lang and pick.paths:
        notices.emit(f"sottotitoli {lang} attivati")


def report_unverified(pick: SubsPick, *, hint: str) -> None:
    """Say when the delivered track is a language guess (`match == "lang"`): neither hash-
    matched nor aligned to the audio, so it may run early or late. Called once the final
    pick is known (after any local alignment) — `hint` names the fix on that path."""
    if pick.paths and pick.match == "lang":
        notices.emit(
            f"sottotitoli scelti per lingua: sincronia non verificata ({hint})",
            code="subs_unverified",
            level="info",
        )


# Embedded subtitle codecs a WebVTT rendition can carry (text). Bitmap tracks (PGS, VobSub,
# DVB) need OCR and never qualify.
_TEXT_SUB_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"})


def embedded_pick(tr: tracks.Tracks, langs: list[str]) -> tuple[int, str] | None:
    """The embedded text subtitle to deliver: `(subtitle-relative index, language code)`
    for the first full (non-`forced`) text track in `langs` order, or None. A `forced`
    track holds only the foreign-language lines — the first ita track of a release can be
    one, and the viewer then sees nothing for normal speech (field 2026-10-02, ADR 0042)."""
    for want in langs:
        for i, t in enumerate(tr.subs):
            if t.forced or t.codec not in _TEXT_SUB_CODECS:
                continue
            if languages.normalize(t.lang) == want or t.lang == want:
                return i, want
    return None


def stream_filename(stream: Stream) -> str | None:
    """The release filename Torrentio exposes in behaviorHints — the `filename` extra of
    a subtitles hash query (improves matching per the OpenSubtitles guidance)."""
    hints = stream.get("behaviorHints")
    name = hints.get("filename") if isinstance(hints, dict) else None
    return name if isinstance(name, str) and name else None


# A subtitle is tens of KB; these bound a hostile or broken provider (a gzip bomb).
_SUB_MAX_BYTES = 2 * 1024 * 1024
_SUB_MAX_DECODED = 8 * 1024 * 1024
# Downloads are immutable per URL: re-casting a title, or the alignment walking the
# alternates again, must not re-fetch them. Bounded like the poster cache.
_SUB_CACHE_MAX_FILES = 300
_SUB_CACHE_MAX_BYTES = 30 * 1024 * 1024


def _sub_cache_path(url: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "nstream" / "subs" / hashlib.sha256(url.encode()).hexdigest()


def _fetch_subtitle(url: str) -> bytes | None:
    """The decompressed bytes of one subtitle download, None on failure or over a cap."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": api.UA})
        with urllib.request.urlopen(req, timeout=api.TIMEOUT) as resp:
            raw = resp.read(_SUB_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _SUB_MAX_BYTES:
        _log.warning("sottotitolo oltre %d byte: scartato", _SUB_MAX_BYTES)
        return None
    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
                out = gz.read(_SUB_MAX_DECODED + 1)
        except (OSError, EOFError):
            return raw if raw[:2] != b"\x1f\x8b" else None
        if len(out) > _SUB_MAX_DECODED:
            _log.warning("sottotitolo decompresso oltre %d byte: scartato", _SUB_MAX_DECODED)
            return None
        raw = out
    return raw


def _download_subtitle(sub: Subtitle, work_dir: str) -> str | None:
    url = sub.get("url")
    if not url:
        return None
    cached = _sub_cache_path(url)
    try:
        raw: bytes | None = cached.read_bytes()
    except OSError:
        raw = _fetch_subtitle(url)
        if raw is None:
            notices.emit("download sottotitolo fallito")
            return None
        with contextlib.suppress(OSError):
            util.atomic_write_bytes(cached, raw, prefix=".sub-")
            util.prune_lru(
                cached.parent, max_files=_SUB_CACHE_MAX_FILES, max_bytes=_SUB_CACHE_MAX_BYTES
            )
    # A per-play copy in the temp dir: retiming rewrites it in place, the cache stays pristine.
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
    choose: Choose | None = None,
) -> tuple[str, ...]:
    """Back-compat façade over `_pick` for callers that only need the file paths
    (the interactive menu). The no-menu paths use `auto_subs` → SubsPick."""
    return _pick(
        cfg, typ, video_id, work_dir, mode=mode, lang=lang, video_url=video_url, choose=choose
    ).paths


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
    choose: Choose | None = None,
) -> SubsPick:
    """Fetch, rank and download one subtitle track. `mode="menu"` asks through the injected
    `choose` (ADR 0037); without one it falls back to the automatic pick. With `video_url`
    the stream's OSHash is computed first (two 64 KB ranged reads) and hash-matched tracks —
    timed for the exact file — win within a language (ADR 0018). Language stays the primary key: a
    perfectly synced track in the wrong language helps nobody."""
    video_hash: str | None = None
    video_size = 0
    # Loopback = the local P2P gateway (TorrServer): a tail Range read there forces the
    # torrent's LAST piece at startup, the exact anti-pattern piece-deadline scheduling
    # avoids. Hash only direct (debrid/CDN) urls, where a ranged read is cheap.
    if video_url and not subalign.is_loopback(video_url) and (hashed := oshash.hash_url(video_url)):
        video_hash, video_size = hashed
    try:
        subs = api.subtitles(
            cfg, typ, video_id,
            video_hash=video_hash, video_size=video_size, filename=filename,
        )  # fmt: skip
    except api.NetworkError as e:
        notices.emit(f"{e}")
        return SubsPick()
    if not subs:
        notices.emit("nessun sottotitolo")
        return SubsPick()
    langs = [lang] if lang else cfg.subtitle_langs
    pref = {code: i for i, code in enumerate(langs)}
    subs.sort(key=lambda s: (pref.get(s.get("lang", ""), len(pref)), not s.get("hash_match")))
    if mode == "menu" and choose is not None:
        items = [
            (
                f"{s.get('lang', '?'):5s} {s.get('id', '')}"
                + ("  · sync verificata (hash)" if s.get("hash_match") else ""),
                s,
            )
            for s in subs
        ]
        chosen = choose(items, "sottotitoli> ")
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
        notices.emit("nessun sottotitolo nelle lingue preferite")
        return SubsPick()
    return pick


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
    # Walk the pool: a download that fails moves on to the next candidate (it used to end
    # the pick with up to five alternates unused). Hash matches sort first in a language.
    for i, cand in enumerate(pool):
        path = _download_subtitle(cand, work_dir)
        if not path:
            continue
        if cand.get("hash_match"):
            notices.emit("sottotitoli sincronizzati al file (hash-match)")
            return SubsPick((path,), "hash", lang=best_lang)
        return SubsPick((path,), "lang", alternates=tuple(pool[i + 1 :]), lang=best_lang)
    return SubsPick()


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
    tr = tracks.probe_tracks(media_path)
    duration = tr.duration
    # The pass demuxes the whole container and decodes ONE downmixed 16 kHz audio channel
    # (`subalign._mono_chain`): ~4 ms of wall time per second of runtime (measured 2.4 s
    # per 600 s), plus the demux of the file. cfg.sub_align_budget_s is the floor; the
    # timeout grows with runtime and size so a long or big remux is analyzed, not refused.
    try:
        size = os.path.getsize(media_path)
    except OSError:
        size = 0
    timeout = max(float(cfg.sub_align_budget_s), duration * 0.02 + size / 100e6 + 30.0)
    channels = tr.audio[0].channels if tr.audio else None
    fp = subalign.probe_local(media_path, duration=duration, timeout_s=timeout, channels=channels)
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
        notices.emit(
            f"sottotitoli allineati all'audio del file (offset {offset:+.1f}s)",
        )
        return SubsPick((path,), "audio", offset_s=offset, lang=pick.lang)
    return pick


@log.phase("subs")
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
            choose=opts.choose,
        )  # fmt: skip
    if pick.paths and (opts.sub_offset or opts.sub_scale != 1.0):
        for p in pick.paths:
            retime_srt(p, opts.sub_offset, opts.sub_scale)
        notices.emit(
            f"sottotitoli ritimati (offset {opts.sub_offset:+.2f}s"
            + (f", scala {opts.sub_scale:.4f}" if opts.sub_scale != 1.0 else "")
            + ")",
        )
    return pick
