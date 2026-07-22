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

The probe is memoized per url for the process lifetime: a single play flows through up to
three call sites that probe the SAME url (the audio-language guard, the cast vetting, the
pre-remux metadata read), and each network ffprobe is a ranged HTTP read with a 20s budget.
No TTL — the process lives one playback. Failures are cached too: retrying a url that just
failed/timed out would re-pay the full timeout within the same run for no realistic gain.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import util


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
    # Container-level metadata from the same probe, so the Tier-2 remux (`remux._probe_meta`)
    # never needs a second ffprobe over the network: video-stream count (≥2 flags a
    # Dolby-Vision dual-layer source) and duration in seconds (feeds the progress line).
    n_video: int = 0
    duration: float = 0.0
    # Real codec of the first video stream ("" = no video/probe failed) — the cast video
    # vetting (`stream_select.vet_cast_video`, ADR 0017) reads it from the same probe.
    video_codec: str = ""
    # Raw ffprobe `format_name` (e.g. "matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2") — the cast
    # container vetting (`stream_select.vet_cast_container`, ADR 0022) reads it from the same
    # probe to confirm the filename extension (a lying .mp4 that is really Matroska).
    container: str = ""

    def empty(self) -> bool:
        return not self.audio and not self.subs


def _parse_ffprobe(data: dict) -> Tracks:
    """Build per-type 1-based track lists (plus video count / duration) from ffprobe's JSON."""
    audio: list[Track] = []
    subs: list[Track] = []
    n_video = 0
    video_codec = ""
    for s in data.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video":
            n_video += 1
            if not video_codec:
                video_codec = str(s.get("codec_name") or "")
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
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return Tracks(
        audio=audio, subs=subs, n_video=n_video, duration=duration,
        video_codec=video_codec, container=str(fmt.get("format_name") or ""),
    )  # fmt: skip


# Per-url probe memo (see module docstring): failures (empty Tracks) are cached on purpose.
# `timeout` is not part of the key — every caller uses the default FFPROBE_TIMEOUT.
_cache: dict[str, Tracks] = {}


def clear_cache() -> None:
    """Drop the per-url probe memo (used by tests; harmless to call anytime)."""
    _cache.clear()


def probe_tracks(url: str, *, timeout: float = util.FFPROBE_TIMEOUT) -> Tracks:
    """Probe `url` for embedded audio/subtitle tracks (+ video count / duration). Returns
    empty lists if ffprobe is unavailable or the probe fails (caller falls back to mpv
    defaults). Memoized per url — one network ffprobe per stream per process."""
    cached = _cache.get(url)
    if cached is not None:
        return cached
    cmd = [
        "ffprobe", "-v", "error", "-of", "json", "-show_entries",
        "format=duration,format_name"
        ":stream=index,codec_type,codec_name,channels:stream_tags=language,title",
        url,
    ]  # fmt: skip
    proc = util.run_cmd(cmd, timeout=timeout)
    if proc is None:
        result = Tracks()  # ffprobe missing or timed out
    else:
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            data = {}
        result = _parse_ffprobe(data if isinstance(data, dict) else {})
    _cache[url] = result
    return result
