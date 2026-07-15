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
import urllib.request
from typing import cast as typecast

from . import api, tracks, ui
from .config import Config, PlayOpts, Subtitle
from .labels import audio_summary, sub_summary, track_label
from .picker import fzf


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
) -> tuple[str, ...]:
    try:
        subs = api.subtitles(cfg, typ, video_id)
    except api.NetworkError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return ()
    if not subs:
        print("nstream: nessun sottotitolo", file=sys.stderr)
        return ()
    langs = [lang] if lang else cfg.subtitle_langs
    pref = {code: i for i, code in enumerate(langs)}
    subs.sort(key=lambda s: pref.get(s.get("lang", ""), len(pref)))
    if mode == "menu":
        items = [(f"{s.get('lang', '?'):5s} {s.get('id', '')}", s) for s in subs]
        chosen = fzf(items, "sottotitoli> ")
    else:  # auto: take the best preferred-language track, else skip silently
        chosen = subs[0] if subs[0].get("lang", "") in pref else None
        if chosen is None:
            print("nstream: nessun sottotitolo nelle lingue preferite", file=sys.stderr)
    if not chosen:
        return ()
    path = _download_subtitle(chosen, work_dir)
    return (path,) if path else ()


def to_vtt(srt_path: str) -> str | None:
    """Convert an SRT file to WebVTT, required for a side-loaded Cast caption track. Writes a
    sibling `<name>.vtt` and returns its path (or None on failure). Minimal and safe: prepend the
    `WEBVTT` header and turn the `,` millisecond separator into `.` on cue-timing lines only
    (`-->`), leaving cue identifiers and text untouched. Idempotent-ish: a file already starting
    with `WEBVTT` is copied through unchanged."""
    try:
        with open(srt_path, "rb") as f:
            text = f.read().decode("utf-8-sig", errors="replace")
    except OSError:
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
) -> tuple[str, ...]:  # fmt: skip
    """Subtitle files for the no-menu paths (cast / --play / binge). When `safety_sub_lang`
    is set (the audio isn't in the primary language), fetch that language's subtitles as a
    safety net regardless of `--subs`. Otherwise: the preferred-language OpenSubtitles track
    when subtitles were requested, else none."""
    if safety_sub_lang:
        return pick_subtitles(cfg, typ, video_id, work_dir, mode="auto", lang=safety_sub_lang)
    if not opts.sub_mode:
        return ()
    return pick_subtitles(cfg, typ, video_id, work_dir, mode=opts.sub_mode, lang=opts.sub_lang)
