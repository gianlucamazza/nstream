"""Subtitle acquisition + the pre-play audio/subtitle track menu.

Fetch OpenSubtitles tracks, rank by preferred language, download (gunzip) into the
per-play temp dir. Also owns `choose_tracks` (the fzf menu over ffprobe tracks +
OpenSubtitles) so the orchestrator stays free of leaf-menu code."""

from __future__ import annotations

import contextlib
import gzip
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import cast as typecast

from . import api, oshash, tracks, ui
from .config import Config, PlayOpts, Stream, Subtitle
from .labels import audio_summary, sub_summary, track_label
from .picker import fzf


@dataclass(frozen=True)
class SubsPick:
    """Outcome of the no-menu subtitle acquisition: the downloaded file(s) plus HOW the
    track was chosen — `"hash"` (OSHash match: timed for the exact file being played,
    ADR 0018) or `"lang"` (best preferred-language guess, sync not guaranteed). Reported
    in the headless JSON (`subtitles_match`) so a guess is never presented as a match."""

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
    else:  # auto: take the best preferred-language track, else skip silently
        chosen = subs[0] if subs[0].get("lang", "") in pref else None
        if chosen is None:
            print("nstream: nessun sottotitolo nelle lingue preferite", file=sys.stderr)
    if not chosen:
        return SubsPick()
    path = _download_subtitle(chosen, work_dir)
    if not path:
        return SubsPick()
    match = "hash" if chosen.get("hash_match") else "lang"
    if match == "hash":
        print("nstream: sottotitoli sincronizzati al file (hash-match)", file=sys.stderr)
    return SubsPick((path,), match)


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _decode_sub(path: str) -> str | None:
    """Decode a subtitle file WITHOUT destroying accents: UTF-8 (BOM-aware) first, then
    latin-1 (total: every byte decodes) for the common CP1252/latin-1 Italian tracks —
    the addon's `SubEncoding` field is unreliable, and `errors="replace"` used to turn
    every `è`/`à` into `\ufffd` on the TV. Downstream always re-writes UTF-8 (the WebVTT
    spec REQUIRES it; mpv is happiest with it too)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


# Cue-timing timestamp (`HH:MM:SS,mmm`, `.` accepted) — only lines with `-->` are touched.
_SRT_TS = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def retime_srt(path: str, offset: float, scale: float) -> bool:
    """Retime an SRT in place: t' = t * scale + offset (clamped at 0), on cue-timing lines
    only. `scale` fixes framerate drift (e.g. subs authored for 25 fps on a 23.976 video →
    25/23.976), `offset` a constant shift. Upstream of BOTH delivery paths, so mpv and the
    cast's WebVTT see the same corrected timings (ADR 0018). False on I/O failure."""

    def _shift(m: re.Match[str]) -> str:
        h, mnt, s, ms = int(m[1]), int(m[2]), int(m[3]), int(m[4].ljust(3, "0"))
        t = max(0.0, (h * 3600 + mnt * 60 + s + ms / 1000) * scale + offset)
        whole, milli = divmod(round(t * 1000), 1000)
        mm, ss = divmod(whole, 60)
        hh, mm = divmod(mm, 60)
        return f"{hh:02d}:{mm:02d}:{ss:02d},{milli:03d}"

    text = _decode_sub(path)
    if text is None:
        return False
    out = "\n".join(_SRT_TS.sub(_shift, ln) if "-->" in ln else ln for ln in text.splitlines())
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(out + "\n")
    except OSError:
        return False
    return True


def to_vtt(srt_path: str) -> str | None:
    """Convert an SRT file to WebVTT, required for a side-loaded Cast caption track. Writes a
    sibling `<name>.vtt` and returns its path (or None on failure). Minimal and safe: prepend the
    `WEBVTT` header and turn the `,` millisecond separator into `.` on cue-timing lines only
    (`-->`), leaving cue identifiers and text untouched. Idempotent-ish: a file already starting
    with `WEBVTT` is copied through unchanged."""
    text = _decode_sub(srt_path)
    if text is None:
        return None
    if text.lstrip().startswith("WEBVTT"):
        out = text
    else:
        lines = ["WEBVTT", ""]
        lines += [ln.replace(",", ".") if "-->" in ln else ln for ln in text.splitlines()]
        out = "\n".join(lines) + "\n"
    base = srt_path[:-4] if srt_path.lower().endswith(".srt") else srt_path
    vtt_path = f"{base}.vtt"
    try:
        with open(vtt_path, "w", encoding="utf-8") as f:
            f.write(out)
    except OSError:
        return None
    return vtt_path


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
