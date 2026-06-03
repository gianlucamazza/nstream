"""Hardware-aware stream ranking.

Torrentio returns up to ~150 streams per title, sorted by `qualitysize`, so the first
one is the biggest/most extreme file (often an 8K "AI upscale" or a 60-100GB Dolby
Vision REMUX). Auto-picking it leads to stalls or decode errors on integrated GPUs.

This module parses each stream's metadata, detects what the local GPU can actually
decode (via `vainfo`, cached), and splits streams into a ranked `playable` list and an
`excluded` list (with reasons) — so nstream can auto-pick the best stream the hardware
can really play, while still showing the rest, clearly marked, for manual override.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Stream

_CACHE_VERSION = 1
# Conservative default for integrated GPUs when vainfo is unavailable: H.264, HEVC
# (incl. 10-bit) and VP9 decode, no AV1.
_FALLBACK_CODECS = ("h264", "hevc", "hevc10", "vp9")
_DEFAULT_MAX_RESOLUTION = 2160


# --- stream metadata parsing ---------------------------------------------


@dataclass(frozen=True)
class StreamInfo:
    resolution: int = 0  # 4320/2160/1080/720/480, 0 = unknown
    codec: str = ""  # av1 | hevc | h264 | ""
    hdr: bool = False
    dv: bool = False
    dv_profile: int | None = None
    size_gb: float = 0.0
    seeders: int = 0
    cached: bool = False


_RES_PATTERNS = (
    (re.compile(r"4320p|\b8k\b", re.I), 4320),
    (re.compile(r"2160p|\b4k\b|\buhd\b", re.I), 2160),
    (re.compile(r"1440p|\b2k\b", re.I), 1440),
    (re.compile(r"1080p", re.I), 1080),
    (re.compile(r"720p", re.I), 720),
    (re.compile(r"480p", re.I), 480),
)


def _text(stream: Stream) -> str:
    return f"{stream.get('name', '')}\n{stream.get('title', '')}"


def parse_stream(stream: Stream) -> StreamInfo:
    text = _text(stream)
    resolution = next((res for pat, res in _RES_PATTERNS if pat.search(text)), 0)

    if re.search(r"\bav1\b", text, re.I):
        codec = "av1"
    elif re.search(r"x265|h\.?265|hevc", text, re.I):
        codec = "hevc"
    elif re.search(r"x264|h\.?264|\bavc\b", text, re.I):
        codec = "h264"
    else:
        codec = ""

    dv = bool(re.search(r"\bDV\b|dolby.?vision", text, re.I))
    prof = re.search(r"\bDV[.\s]?P(\d)\b", text, re.I) or re.search(
        r"dolby.?vision.*?profile\s*(\d)", text, re.I
    )
    dv_profile = int(prof.group(1)) if prof else None

    size_gb = 0.0
    if m := re.search(r"💾\s*([\d.]+)\s*GB", text, re.I):
        size_gb = float(m.group(1))
    elif m := re.search(r"💾\s*([\d.]+)\s*MB", text, re.I):
        size_gb = float(m.group(1)) / 1024

    seeders = int(m.group(1)) if (m := re.search(r"👤\s*(\d+)", text)) else 0

    return StreamInfo(
        resolution=resolution,
        codec=codec,
        hdr=bool(re.search(r"\bhdr", text, re.I)),
        dv=dv,
        dv_profile=dv_profile,
        size_gb=size_gb,
        seeders=seeders,
        cached="[RD+]" in (stream.get("name") or ""),
    )


# --- hardware capabilities (vainfo) --------------------------------------


@dataclass(frozen=True)
class Caps:
    codecs: frozenset[str] = field(default_factory=lambda: frozenset(_FALLBACK_CODECS))
    max_resolution: int = _DEFAULT_MAX_RESOLUTION


def _caps_from_vainfo(out: str) -> frozenset[str]:
    """Map VAProfile* VLD (decode) entries to normalized codec names."""
    codecs: set[str] = set()
    for line in out.splitlines():
        if "VAProfile" not in line or "VLD" not in line:
            continue  # only decode entrypoints
        if "AV1" in line:
            codecs.add("av1")
        elif "HEVCMain10" in line or "HEVCMain12" in line:
            codecs.add("hevc10")
            codecs.add("hevc")
        elif "HEVC" in line:
            codecs.add("hevc")
        elif "H264" in line:
            codecs.add("h264")
        elif "VP9" in line:
            codecs.add("vp9")
        elif "VP8" in line:
            codecs.add("vp8")
    return frozenset(codecs)


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "nstream" / "hwcaps.json"


def detect_caps(*, use_cache: bool = True) -> Caps:
    """Detect HW decode capabilities via vainfo (cached). Conservative fallback."""
    path = _cache_path()
    if use_cache:
        try:
            data = json.loads(path.read_text())
            if data.get("version") == _CACHE_VERSION:
                return Caps(
                    codecs=frozenset(data["codecs"]),
                    max_resolution=int(data["max_resolution"]),
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    try:
        proc = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=10)
        codecs = _caps_from_vainfo(proc.stdout + proc.stderr)
    except (OSError, subprocess.SubprocessError):
        codecs = frozenset()
    if not codecs:  # vainfo missing or unparsable → conservative default
        codecs = frozenset(_FALLBACK_CODECS)

    caps = Caps(codecs=codecs, max_resolution=_DEFAULT_MAX_RESOLUTION)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": _CACHE_VERSION,
                    "codecs": sorted(caps.codecs),
                    "max_resolution": caps.max_resolution,
                }
            )
        )
    except OSError:
        pass  # cache is best-effort
    return caps


# --- support check + ranking ---------------------------------------------


def _codec_supported(codec: str, caps: Caps) -> bool:
    if not codec:
        return True  # unknown codec: give the benefit of the doubt (usually h264/hevc)
    if codec == "hevc":
        return "hevc" in caps.codecs or "hevc10" in caps.codecs
    return codec in caps.codecs


def unsupported_reason(info: StreamInfo, caps: Caps, max_resolution: int) -> str | None:
    """Why this stream isn't playable on the current hardware, or None if it is."""
    if max_resolution and info.resolution > max_resolution:
        return "8K" if info.resolution >= 4320 else f"{info.resolution}p"
    if not _codec_supported(info.codec, caps):
        return f"{info.codec.upper()} no-HW"
    if info.dv_profile == 5:
        return "Dolby Vision P5"
    return None


_SEED_BUCKET = 40  # past this many seeders, treat as "well-seeded enough"


def _score(info: StreamInfo) -> tuple:
    # cached first; then resolution; HEVC over H264 at equal res; "well-seeded
    # enough" (bucketed so popularity doesn't force a huge file); then the smaller
    # file (faster streaming start) among equally-seeded options.
    return (
        info.cached,
        info.resolution,
        info.codec == "hevc",
        min(info.seeders, _SEED_BUCKET),
        -info.size_gb,
    )


@dataclass(frozen=True)
class RankedStream:
    stream: Stream
    info: StreamInfo
    reason: str | None = None  # set only for excluded streams


def rank_streams(
    streams: list[Stream], caps: Caps, *, max_resolution: int, allow_software: bool, allow_dv5: bool
) -> tuple[list[RankedStream], list[RankedStream]]:
    """Split streams into (playable_sorted, excluded). `allow_software` keeps codecs
    the GPU can't decode; `allow_dv5` keeps Dolby Vision Profile 5."""
    playable: list[RankedStream] = []
    excluded: list[RankedStream] = []
    for s in streams:
        info = parse_stream(s)
        reason = unsupported_reason(info, caps, max_resolution)
        if reason and allow_software and reason.endswith("no-HW"):
            reason = None
        if reason and allow_dv5 and reason.startswith("Dolby Vision"):
            reason = None
        if reason:
            excluded.append(RankedStream(s, info, reason))
        else:
            playable.append(RankedStream(s, info))
    playable.sort(key=lambda r: _score(r.info), reverse=True)
    return playable, excluded
