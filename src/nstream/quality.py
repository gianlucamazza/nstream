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

_CACHE_VERSION = 2
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
    languages: frozenset[str] = frozenset()  # ISO codes + "multi"; empty = untagged
    source: str = ""  # remux|bluray|webdl|webrip|hdtv|dvd|cam|ts|tc|scr|""
    audio: str = ""  # truehd|dtshd|dts|eac3|ac3|aac|""; "headline" track codec
    release_name: str = ""  # title's first line (torrent filename), for dedup


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


# Release-name language tokens → ISO code (or "multi"). Word-boundary matched so a
# group name like "-CYBER" or "ENG" inside another word doesn't false-positive.
_LANG_TOKENS = {
    "ita": ("ITA", "ITALIAN", "ITALIANO"),
    "eng": ("ENG", "ENGLISH"),
    "fra": ("FRA", "FRENCH", "TRUEFRENCH", "VFF", "VFQ", "VOSTFR"),
    "spa": ("SPA", "ESP", "SPANISH", "CASTELLANO", "LATINO"),
    "deu": ("GER", "GERMAN", "DEU"),
    "rus": ("RUS", "RUSSIAN"),
    "por": ("POR", "PORTUGUESE", "DUBLADO", "LEGENDADO"),
    "multi": ("MULTI", "MULTILANG", "MULTI-LANG", "DUAL", "DUALAUDIO"),
}
_LANG_RE = {
    code: re.compile(r"(?<![A-Za-z])(?:" + "|".join(toks) + r")(?![A-Za-z])", re.I)
    for code, toks in _LANG_TOKENS.items()
}
# Flag emoji Torrentio may prepend → ISO code.
_FLAG_LANG = {
    "🇮🇹": "ita",
    "🇬🇧": "eng",
    "🇺🇸": "eng",
    "🇫🇷": "fra",
    "🇪🇸": "spa",
    "🇩🇪": "deu",
    "🇷🇺": "rus",
    "🇵🇹": "por",
    "🇧🇷": "por",
}  # noqa: E501

# Source/release type tokens, checked in priority order (REMUX wins over BluRay).
_SOURCE_PATTERNS = (
    ("remux", re.compile(r"\bBD-?REMUX\b|\bREMUX\b", re.I)),
    ("cam", re.compile(r"\b(?:HD)?CAM(?:RIP)?\b", re.I)),
    ("ts", re.compile(r"\b(?:HD)?TS\b|\bTELESYNC\b|\bPDVD\b", re.I)),
    ("tc", re.compile(r"\b(?:HD)?TC\b|\bTELECINE\b", re.I)),
    ("scr", re.compile(r"\bSCR\b|\bSCREENER\b|\b[BD]?DVDSCR\b|\bBDSCR\b", re.I)),
    ("bluray", re.compile(r"\bBLU-?RAY\b|\bBD(?:RIP)?\b|\bBRRIP\b", re.I)),
    ("webdl", re.compile(r"\bWEB-?DL\b", re.I)),
    ("webrip", re.compile(r"\bWEB-?RIP\b|\bWEB\b", re.I)),
    ("hdtv", re.compile(r"\bHDTV\b|\bPDTV\b", re.I)),
    ("dvd", re.compile(r"\bDVD-?RIP\b|\bDVD\b", re.I)),
)
_CAMRIP_SOURCES = frozenset({"cam", "ts", "tc", "scr"})

# Audio codec tokens, lossless first: a remux often lists "TrueHD + AC3" but the
# receiver plays the headline (lossless) track, so it's the one that decides Cast
# compatibility. First match wins.
_AUDIO_PATTERNS = (
    ("truehd", re.compile(r"\bTRUE-?HD\b", re.I)),
    ("dtshd", re.compile(r"\bDTS-?HD\b|\bDTS-?MA\b|\bDTS:?X\b", re.I)),
    ("dts", re.compile(r"\bDTS\b", re.I)),
    ("eac3", re.compile(r"\bE-?AC-?3\b|\bDD\+|\bDDP|\bDOLBY\s?DIGITAL\s?PLUS\b", re.I)),
    ("ac3", re.compile(r"\bAC-?3\b|\bDD5\.1\b|\bDOLBY\s?DIGITAL\b", re.I)),
    ("aac", re.compile(r"\bAAC\b", re.I)),
)
# Audio the Chromecast Default Media Receiver cannot decode (silent video).
_CAST_LOSSLESS = frozenset({"truehd", "dtshd", "dts"})

# Torrentio marks an instantly-available (cached) debrid stream with a per-provider
# prefix: [RD+] (RealDebrid), [AD+], [PM+], [TB+], [Putio+]… ("+" = cached, vs
# "[RD download]"). Provider-agnostic so any debrid's cached streams are detected.
_CACHED_RE = re.compile(r"\[[A-Za-z]{2,6}\+\]")


def _parse_languages(text: str) -> frozenset[str]:
    found = {code for code, pat in _LANG_RE.items() if pat.search(text)}
    found |= {code for flag, code in _FLAG_LANG.items() if flag in text}
    return frozenset(found)


def _parse_source(text: str) -> str:
    return next((name for name, pat in _SOURCE_PATTERNS if pat.search(text)), "")


def _parse_audio(text: str) -> str:
    return next((name for name, pat in _AUDIO_PATTERNS if pat.search(text)), "")


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
        cached=bool(_CACHED_RE.search(stream.get("name") or "")),
        languages=_parse_languages(text),
        source=_parse_source(text),
        audio=_parse_audio(text),
        release_name=(stream.get("title") or "").split("\n", 1)[0].strip(),
    )


# --- hardware capabilities (vainfo) --------------------------------------


@dataclass(frozen=True)
class Caps:
    codecs: frozenset[str] = field(default_factory=lambda: frozenset(_FALLBACK_CODECS))
    max_resolution: int = _DEFAULT_MAX_RESOLUTION
    # True only when a real vainfo probe succeeded → VAAPI decode is available, so
    # mpv's `vaapi` hwdec is the reliable HW path (vs the conservative fallback).
    vaapi: bool = False


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
                    vaapi=bool(data["vaapi"]),
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    try:
        proc = subprocess.run(["vainfo"], capture_output=True, text=True, timeout=10)
        probed = _caps_from_vainfo(proc.stdout + proc.stderr)
    except (OSError, subprocess.SubprocessError):
        probed = frozenset()
    # A real probe means VAAPI is usable; otherwise fall back conservatively and
    # mark vaapi unavailable so we don't force mpv onto a path we couldn't verify.
    vaapi = bool(probed)
    codecs = probed or frozenset(_FALLBACK_CODECS)

    caps = Caps(codecs=codecs, max_resolution=_DEFAULT_MAX_RESOLUTION, vaapi=vaapi)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": _CACHE_VERSION,
                    "codecs": sorted(caps.codecs),
                    "max_resolution": caps.max_resolution,
                    "vaapi": caps.vaapi,
                }
            )
        )
    except OSError:
        pass  # cache is best-effort
    return caps


def preferred_hwdec(caps: Caps) -> str | None:
    """The mpv hwdec method to force for this GPU, or None to leave mpv's choice.

    Returns ``vaapi`` when a real VAAPI probe succeeded (Intel/AMD): it's the mature
    HW path and avoids mpv probing experimental Vulkan decode (unsupported on many
    iGPUs) or a missing CUDA. NVIDIA-only setups aren't vainfo-detectable here, so we
    return None and let mpv decide. Verified live on Iris Xe: `vaapi` decodes zero-copy
    cleanly even under `gpu-api=vulkan`."""
    return "vaapi" if caps.vaapi else None


def cast_caps() -> Caps:
    """Decode profile for a Chromecast/Google TV receiver — NOT the laptop GPU.

    Casting plays on the TV, so the laptop's vainfo codecs are irrelevant. A modern
    Google TV decodes H.264/HEVC(+10bit)/VP9 up to 4K; AV1 is not guaranteed on older
    models, so it's left out (such streams drop to the ⚠ section, still pickable)."""
    return Caps(
        codecs=frozenset({"h264", "hevc", "hevc10", "vp9"}),
        max_resolution=2160,
        vaapi=False,
    )


# --- support check + ranking ---------------------------------------------


def _codec_supported(codec: str, caps: Caps) -> bool:
    if not codec:
        return True  # unknown codec: give the benefit of the doubt (usually h264/hevc)
    if codec == "hevc":
        return "hevc" in caps.codecs or "hevc10" in caps.codecs
    return codec in caps.codecs


def unsupported_reason(
    info: StreamInfo,
    caps: Caps,
    max_resolution: int,
    *,
    audio_langs: tuple[str, ...] = (),
    lang_filter: bool = False,
    exclude_camrip: bool = False,
    min_seeders: int = 0,
    cast_audio: bool = False,
) -> str | None:
    """Why this stream is excluded from the main list, or None if it belongs there.
    Order: hardware (codec/resolution/DV5) → Cast audio → camrip → language → near-dead.
    The extra filters are opt-in (defaults are no-ops) so the HW-only behaviour and
    existing callers are unchanged."""
    if max_resolution and info.resolution > max_resolution:
        return "8K" if info.resolution >= 4320 else f"{info.resolution}p"
    if not _codec_supported(info.codec, caps):
        return f"{info.codec.upper()} no-HW"
    if info.dv_profile == 5:
        return "Dolby Vision P5"
    # Cast: the Default Media Receiver can't decode TrueHD/DTS/DTS-HD → silent audio.
    # A REMUX carries the lossless track even when the title omits the codec, so it's
    # demoted too; other unknown audio gets the benefit of the doubt (WEB-DLs rarely tag).
    if cast_audio:
        if info.audio in _CAST_LOSSLESS:
            return f"audio {info.audio.upper()}"
        if info.source == "remux":
            return "audio remux"
    if exclude_camrip and info.source in _CAMRIP_SOURCES:
        return f"camrip ({info.source})"
    # Tagged with languages but none preferred (and not a multi-language release).
    if (
        lang_filter
        and audio_langs
        and info.languages
        and "multi" not in info.languages
        and not (info.languages & set(audio_langs))
    ):
        return "lingua " + "/".join(sorted(info.languages))
    # Non-cached torrent with too few seeders may never start (cached [RD+] are exempt).
    if min_seeders and not info.cached and info.seeders < min_seeders:
        return "pochi seeder"
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


def _dedup_by_release(
    infos: list[tuple[Stream, StreamInfo]],
) -> list[tuple[Stream, StreamInfo]]:
    """Collapse the same release seen on multiple trackers (identical release_name),
    keeping the best-scoring copy. Streams without a release_name are kept as-is."""
    best: dict[str, tuple[Stream, StreamInfo]] = {}
    out: list[tuple[Stream, StreamInfo]] = []
    for s, info in infos:
        key = info.release_name.lower()
        if not key:
            out.append((s, info))
            continue
        cur = best.get(key)
        if cur is None or _score(info) > _score(cur[1]):
            best[key] = (s, info)
    out.extend(best.values())
    return out


def rank_streams(
    streams: list[Stream],
    caps: Caps,
    *,
    max_resolution: int,
    allow_software: bool,
    allow_dv5: bool,
    audio_langs: tuple[str, ...] = (),
    lang_filter: bool = False,
    exclude_camrip: bool = False,
    min_seeders: int = 0,
    dedup: bool = False,
    cast_audio: bool = False,
) -> tuple[list[RankedStream], list[RankedStream]]:
    """Split streams into (playable_sorted, excluded). `allow_software` keeps codecs
    the GPU can't decode; `allow_dv5` keeps Dolby Vision Profile 5. The opt-in filters
    (lang_filter/exclude_camrip/min_seeders) move non-matching streams to `excluded`
    with a reason; `dedup` drops duplicate releases entirely (not in either list)."""
    infos = [(s, parse_stream(s)) for s in streams]
    if dedup:
        infos = _dedup_by_release(infos)
    playable: list[RankedStream] = []
    excluded: list[RankedStream] = []
    for s, info in infos:
        reason = unsupported_reason(
            info, caps, max_resolution,
            audio_langs=audio_langs, lang_filter=lang_filter,
            exclude_camrip=exclude_camrip, min_seeders=min_seeders,
            cast_audio=cast_audio,
        )  # fmt: skip
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
