"""Presentation helpers: turn domain dicts into fzf/mpv display strings.

Pure formatting, no flow. Reads the active terminal capabilities on demand via
`ui.active_caps()` (pinned once by the orchestrator's `_init_theme`), so callers
need not thread glyphs/palette through every label call."""

from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime

from . import quality, tracks, ui
from .types import HistoryEntry, Meta, Stream, Video


def meta_label(m: Meta, *, watchlisted: bool = False) -> str:
    caps = ui.active_caps()
    g, pal = ui.glyphs(caps), ui.palette(caps)
    icon = g.series if m.get("type") == "series" else g.movie
    info = m.get("releaseInfo", "")
    year = f"  {ui.ansi(f'({info})', pal.dim)}" if info else ""
    star = f"{g.star} " if watchlisted else ""
    label = f"{star}{icon}  {ui.ansi(m.get('name', '?'), pal.accent)}{year}"
    # Cheap hint from the slim catalog (year only): flag titles from a future year.
    # Same-year-but-unreleased titles are caught precisely at selection time.
    yr = re.match(r"(\d{4})", str(info))
    if yr and int(yr.group(1)) > datetime.now(UTC).year:
        label += "  " + ui.ansi(f"· {g.movie} in uscita", pal.warn)
    rating = m.get("imdbRating")
    if rating:
        label += "  " + ui.ansi(f"★ {rating}", pal.dim)
    genres = m.get("genres")
    if isinstance(genres, list) and genres:
        label += "  " + ui.ansi(" · ".join(str(x) for x in genres[:2]), pal.dim)
    return label


def stream_label(s: Stream, info: quality.StreamInfo | None = None) -> str:
    caps = ui.active_caps()
    g, pal = ui.glyphs(caps), ui.palette(caps)
    # Cap to the terminal width on narrow panes; floor 60 keeps the tag prefix readable.
    cols = shutil.get_terminal_size((80, 24)).columns
    cap = min(200, max(60, cols - 2))
    # Release names are untrusted: strip control/ESC chars before they reach the terminal.
    name = ui.sanitize(s.get("name") or "").replace("\n", " ")
    title = ui.sanitize(s.get("title") or "").replace("\n", " · ")
    base = f"{name}  |  {title}"
    # Provenance badge when api.streams stamped the source addon (multi-source clarity).
    addon = ui.sanitize(s.get("addon") or "").strip()
    if addon:
        base = f"[{addon}] {base}"
    base = base[:cap]
    if info is None:
        return base
    tags = []
    if info.resolution:
        tags.append(f"{info.resolution}p")
    if info.codec:
        tags.append(info.codec)
    if info.dv:
        tags.append("DV")
    elif info.hdr:
        tags.append("HDR")
    if info.source:
        tags.append(info.source)
    if info.audio:
        tags.append(info.audio.upper())
    if info.languages:
        tags.append("/".join(sorted(info.languages)))
    if info.size_gb:
        tags.append(f"{info.size_gb:.1f}G")
    prefix = (g.cached if info.cached else " ") + " " + " ".join(tags)
    # Truncate the plain string first, then colour only the fixed-width prefix region so
    # the alignment is exact and no ANSI escape is ever cut by the width cap.
    pref_w = min(36, cap)
    plain = f"{prefix:{pref_w}s} {base}"[:cap]
    return ui.ansi(plain[:pref_w], pal.good if info.cached else pal.dim) + plain[pref_w:]


def episode_label(v: Video) -> str:
    caps = ui.active_caps()
    g, pal = ui.glyphs(caps), ui.palette(caps)
    tag = ui.ansi(f"S{v.get('season', 0):02d}E{v.get('episode', 0):02d}", pal.dim)
    return f"{g.series}  {tag}  {v.get('name', '')}".rstrip()


def history_label(e: HistoryEntry, *, next_episode: bool = False) -> str:
    """One continue-watching row. `next_episode` marks an entry that continues with the
    FOLLOWING episode rather than itself — the caller decides that (it owns `state`);
    this stays a presentation function with no policy of its own."""
    caps = ui.active_caps()
    g, pal = ui.glyphs(caps), ui.palette(caps)
    # Same type glyph as meta_label so home / continue-watching stay scannable.
    icon = g.series if e.get("type") == "series" else g.movie
    title = f"{icon}  {ui.ansi(e.get('title', '?'), pal.secondary)}"
    if e.get("type") == "series" and e.get("season"):
        title += "  " + ui.ansi(f"S{e.get('season', 0):02d}E{e.get('episode', 0):02d}", pal.dim)
    if next_episode:
        return title + "  " + ui.ansi("→ prossimo episodio", pal.accent)
    dur = e.get("duration") or 0.0
    pos = e.get("position", 0.0)
    if dur:
        bar = ui.progress_bar(pos, dur, width=12, caps=caps)
        title += "  " + ui.ansi(f"{bar} {pos / dur * 100:.0f}%", pal.accent)
    return title


def display_title(name: str, video: Video | None) -> str:
    """The media title shown by mpv (OSC, window, taskbar)."""
    if video is None:
        return name
    label = f"{name} · S{video.get('season', 0):02d}E{video.get('episode', 0):02d}"
    epname = video.get("name")
    return f"{label} · {epname}" if epname else label


def track_label(t: tracks.Track) -> str:
    parts = [t.lang or "und"]
    if t.codec:
        parts.append(t.codec)
    if t.channels:
        parts.append(f"{t.channels}ch")
    if t.title:
        parts.append(f'"{t.title}"')
    return " · ".join(parts)


def audio_summary(aid: int | None, tr: tracks.Tracks) -> str:
    if aid is None:
        return "automatico (lingua preferita)"
    t = next((a for a in tr.audio if a.id == aid), None)
    return track_label(t) if t else f"traccia {aid}"


def sub_summary(sid: int | str | None, sub_paths: tuple[str, ...], tr: tracks.Tracks) -> str:
    if sub_paths:
        return "OpenSubtitles (esterni)"
    if sid in (None, "no"):
        return "nessuno"
    t = next((s for s in tr.subs if s.id == sid), None)
    return track_label(t) if t else f"traccia {sid}"
