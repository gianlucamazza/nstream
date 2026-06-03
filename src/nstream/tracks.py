"""Probe a media URL for its embedded audio/subtitle tracks via ffprobe.

The streams nstream plays come from Real-Debrid as direct HTTP URLs. ffprobe reads
the container header (a ranged read) and reports the tracks, so nstream can offer a
pre-play menu to pick the exact audio/subtitle track instead of relying only on mpv's
language-preference auto-selection.

mpv addresses tracks per type with 1-based ids (--aid/--sid), assigned in demux order.
ffprobe lists streams in file order, so the Nth audio stream maps to mpv aid=N (same for
subtitles). This holds for single-file containers (mkv/mp4), which is what we play.

ffprobe is optional: if it's missing or the probe fails, this returns empty lists and the
caller falls back to mpv's defaults. The command line (which embeds the RD token in the
URL) is never logged.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Track:
    id: int  # mpv per-type id (1-based), i.e. position among tracks of this type
    lang: str = "und"
    codec: str = ""
    channels: int | None = None
    title: str = ""


@dataclass(frozen=True)
class Tracks:
    audio: list[Track] = field(default_factory=list)
    subs: list[Track] = field(default_factory=list)

    def empty(self) -> bool:
        return not self.audio and not self.subs


def _parse_ffprobe(data: dict) -> Tracks:
    """Build per-type 1-based track lists from ffprobe's JSON stream list."""
    audio: list[Track] = []
    subs: list[Track] = []
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind not in ("audio", "subtitle"):
            continue
        bucket = audio if kind == "audio" else subs
        tags = s.get("tags") or {}
        chans = s.get("channels")
        track = Track(
            id=len(bucket) + 1,
            lang=str(tags.get("language") or "und"),
            codec=str(s.get("codec_name") or ""),
            channels=int(chans) if isinstance(chans, int) else None,
            title=str(tags.get("title") or ""),
        )
        bucket.append(track)
    return Tracks(audio=audio, subs=subs)


def probe_tracks(url: str, *, timeout: float = 20.0) -> Tracks:
    """Probe `url` for embedded audio/subtitle tracks. Returns empty lists if ffprobe
    is unavailable or the probe fails (caller falls back to mpv defaults)."""
    cmd = [
        "ffprobe", "-v", "error", "-of", "json",
        "-show_entries", "stream=index,codec_type,codec_name,channels:stream_tags=language,title",
        url,
    ]  # fmt: skip
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return Tracks()  # ffprobe missing or timed out
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return Tracks()
    return _parse_ffprobe(data if isinstance(data, dict) else {})
