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

import contextlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from . import languages, sources, util
from .config import Config
from .types import Stream

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
    container: str = ""  # mp4|mkv|webm|avi|mpegts|""; from the release filename extension
    release_name: str = ""  # title's first line (torrent filename), for dedup
    info_hash: str = ""  # pure-torrent streams only; resolved to a url by the engine
    file_idx: int | None = None  # which file in the torrent (None = largest)
    has_url: bool = False  # a ready debrid url: playable without touching the swarm


# An explicit "NNNNp" wins over the 8K/4K/UHD/2K aliases: "UHD.BluRay.1080p" is a 1080p
# encode of a UHD source and "Remastered.4K.1080p" a 1080p file.
_RES_PATTERNS = (
    (re.compile(r"4320p", re.I), 4320),
    (re.compile(r"2160p", re.I), 2160),
    (re.compile(r"1440p", re.I), 1440),
    (re.compile(r"1080p", re.I), 1080),
    (re.compile(r"720p", re.I), 720),
    (re.compile(r"480p", re.I), 480),
    (re.compile(r"\b8k\b", re.I), 4320),
    (re.compile(r"\b4k\b|\buhd\b", re.I), 2160),
    (re.compile(r"\b2k\b", re.I), 1440),
)


def _release_filename(stream: Stream) -> str:
    """`behaviorHints.filename` — the protocol's canonical identity for the release file."""
    hints = stream.get("behaviorHints")
    return (hints.get("filename") or "") if isinstance(hints, dict) else ""


def _text(stream: Stream) -> str:
    """Free-text corpus for the heuristic parsers (resolution, codec, languages, source…).

    The union of every field that can carry release words, not one chosen field (ADR 0026):
    `description` is the protocol's current headline, `title` its deprecated predecessor
    (still populated by Torrentio), and the filename often carries tags — `[Esp]`, `BluRay` —
    that neither headline repeats. Reading only one of them made every release word invisible
    for any addon that had migrated.
    """
    return "\n".join(
        (
            stream.get("name") or "",
            stream.get("description") or "",
            stream.get("title") or "",
            _release_filename(stream),
        )
    )


# Release-name language tokens → ISO code, derived from the single language registry.
# Word-boundary matched so a group name like "-CYBER" or "ENG" inside another word doesn't
# false-positive.
_LANG_TOKENS = {lang.code: lang.tokens for lang in languages.LANGUAGES}
# Short codes that are also ordinary title words ("Chi ha ucciso…", "Lat", "Por", "Sk"):
# counted only when written in capitals, as release language tags are. Unambiguous tags
# (ITA, ENG, iTALiAN…) stay case-insensitive.
_AMBIGUOUS_TOKENS = frozenset(
    {"CHI", "POR", "LAT", "SK", "CZ", "ARA", "SPA", "TUR", "HIN", "POL", "DUT", "JAP", "ESP", "KOR"}
)


def _lang_re(toks: tuple[str, ...]) -> re.Pattern[str]:
    plain = [t for t in toks if t not in _AMBIGUOUS_TOKENS]
    caps = [t for t in toks if t in _AMBIGUOUS_TOKENS]
    alts = [f"(?i:{'|'.join(plain)})"] if plain else []
    alts += caps  # case-sensitive: only the all-caps tag form
    return re.compile(r"(?<![A-Za-z])(?:" + "|".join(alts) + r")(?![A-Za-z])")


_LANG_RE = {code: _lang_re(toks) for code, toks in _LANG_TOKENS.items()}
# Flag emoji Torrentio may prepend → ISO code.
_FLAG_LANG = {flag: lang.code for lang in languages.LANGUAGES for flag in lang.flags}

# Source/release type tokens, checked in priority order (REMUX wins over BluRay).
_SOURCE_PATTERNS = (
    ("remux", re.compile(r"\bBD-?REMUX\b|\bREMUX\b", re.I)),
    ("cam", re.compile(r"\b(?:HD)?CAM(?:RIP)?\b", re.I)),
    ("ts", re.compile(r"\b(?:HD)?TS\b|\bTELESYNC\b|\bPDVD\b", re.I)),
    ("tc", re.compile(r"\b(?:HD)?TC\b|\bTELECINE\b", re.I)),
    ("scr", re.compile(r"\bSCR\b|\bSCREENER\b|\b[BD]?DVDSCR\b|\bBDSCR\b|\b(?:HD)?TSCR\b", re.I)),
    # DCP/DCPRip = a rip of a Digital Cinema Package — a cinema leak, same class as CAM/TS.
    ("dcp", re.compile(r"\bDCP-?RIP\b|\bDCP\b", re.I)),
    ("bluray", re.compile(r"\bBLU-?RAY\b|\bBD(?:RIP)?\b|\bBRRIP\b", re.I)),
    ("webdl", re.compile(r"\bWEB-?DL\b", re.I)),
    ("webrip", re.compile(r"\bWEB-?RIP\b|\bWEB\b", re.I)),
    ("hdtv", re.compile(r"\bHDTV\b|\bPDTV\b", re.I)),
    ("dvd", re.compile(r"\bDVD-?RIP\b|\bDVD\b", re.I)),
)
_CAMRIP_SOURCES = frozenset({"cam", "ts", "tc", "scr", "dcp"})

# Audio codec tokens, lossless first: a remux often lists "TrueHD + AC3" but the
# receiver plays the headline (lossless) track, so it's the one that decides Cast
# compatibility. First match wins.
_AUDIO_PATTERNS = (
    ("truehd", re.compile(r"\bTRUE-?HD(?![A-Za-z])", re.I)),
    ("dtshd", re.compile(r"\bDTS-?HD(?![A-Za-z])|\bDTS-?MA(?![A-Za-z])|\bDTS:?X\b", re.I)),
    ("dts", re.compile(r"\bDTS(?![A-Za-z])", re.I)),
    ("eac3", re.compile(r"\bE-?AC-?3(?![A-Za-z])|\bDD\+|\bDDP|\bDOLBY\s?DIGITAL\s?PLUS\b", re.I)),
    ("ac3", re.compile(r"\bAC-?3(?![A-Za-z])|\bDD5\.1\b|\bDOLBY\s?DIGITAL\b", re.I)),
    # `(?![A-Za-z])` instead of a trailing \b: release names glue the channel layout to
    # the codec ("AAC2.0", "DTS5.1", "TrueHD7.1"), where \b never matches.
    ("aac", re.compile(r"\bAAC(?![A-Za-z])", re.I)),
)
# Audio the Chromecast Default Media Receiver never decodes → excluded for cast (silent).
_CAST_LOSSLESS = frozenset({"truehd", "dtshd", "dts"})
# Audio the Default Media Receiver actually DECODES (not the TV hardware): per Google Cast
# docs — HE/LC-AAC, MP3, Opus, FLAC, Vorbis, LPCM. AC-3/E-AC-3 are *passthrough only*
# (sink-dependent, the DMR doesn't reliably enable it → often silent), so they rank below
# decodable/unknown for cast instead of being chosen first. See cast-audio model (L1).
_CAST_DECODABLE = frozenset({"aac", "mp3", "opus", "flac", "vorbis", "lpcm"})
# Audio the receiver can't decode → a Tier-2 cast needs a host remux (mirrors
# `remux._UNDECODABLE`). Used to apply the remux-only resolution cap (a 4K remux is a huge
# download, while a direct 4K cast is free), so only these candidates are capped.
_CAST_NEEDS_REMUX = frozenset({"ac3", "eac3", "dts", "dtshd", "truehd"})


def _cast_audio_rank(info: StreamInfo) -> int:
    """Cast audio preference for the score (higher = better): 2 = the receiver decodes it
    natively (AAC…); 1 = untagged (unknown, benefit of the doubt); 0 = AC-3/E-AC-3
    (passthrough-only, frequently silent on the Default Media Receiver)."""
    if info.audio in _CAST_DECODABLE:
        return 2
    if not info.audio:
        return 1
    return 0


# Above this size an untagged release is a full disc or remux: only lossless audio
# (TrueHD/DTS-HD) and untouched video get that big. Field case 2026-10-01: an 87GB
# "UHD Blu-ray disc" with no codec in its name ranked first for cast, and sailed past the
# remux size budget because nothing marked it as needing a remux.
_DISC_SIZE_GB = 30.0


def _likely_needs_remux(info: StreamInfo) -> bool:
    """True when casting this release will probably need a Tier-2 host remux — so its
    download cost (size) matters for ranking. Known-undecodable codecs need it; so does a
    REMUX whose name omits the codec, because a remux carries the untouched lossless disc
    track (TrueHD/DTS-HD) even when untagged — the same reasoning `unsupported_reason` uses
    to demote unlabelled remuxes. A definitive answer only comes from the cast-time ffprobe;
    this is the ranking heuristic that keeps a huge unlabelled 4K remux from out-ranking a
    modest alternative (it would otherwise look like decodable/unknown audio).

    A DMR-incompatible container (.mkv/.avi, ADR 0022) also needs a Tier-2 rewrap to MP4, so
    it counts too: this demotes a 4K mkv so an mp4 alternative out-ranks it and the common
    path stays a direct mp4 cast (the container twin of "selection prefers AAC")."""
    return (
        info.audio in _CAST_NEEDS_REMUX
        or (not info.audio and (info.source == "remux" or info.size_gb >= _DISC_SIZE_GB))
        or (info.container != "" and info.container not in CAST_CONTAINER_DECODABLE)
    )


# Shared with `api` (single definition in `sources`).
_CACHED_RE = sources.CACHED_MARKER_RE


# Subtitle-language tags ("SUB.ITA", "Subs ENG FRE") name the subtitles, not the audio:
# dropped before matching so a subbed release isn't ranked as a dub.
_SUB_TAG_RE = re.compile(
    r"(?<![A-Za-z])SUB(?:S|BED|TITLES?)?(?:[ ._/-]+(?:"
    + "|".join(t for toks in _LANG_TOKENS.values() for t in toks)
    + r")(?![A-Za-z]))+",
    re.I,
)


def _parse_languages(text: str) -> frozenset[str]:
    text = _SUB_TAG_RE.sub(" ", text)
    found = {code for code, pat in _LANG_RE.items() if pat.search(text)}
    found |= {code for flag, code in _FLAG_LANG.items() if flag in text}
    return frozenset(found)


def _parse_source(text: str) -> str:
    return next((name for name, pat in _SOURCE_PATTERNS if pat.search(text)), "")


# Filename extension → canonical container token. The container lives ONLY in the release
# filename (`behaviorHints.filename`), never in the name+title text `_text` parses — so this
# is the one parser that reads `behaviorHints`. Used for cast-container vetting (ADR 0022):
# the Default Media Receiver loads MP4/WebM/CMAF but refuses Matroska (.mkv).
_CONTAINER_BY_EXT = {
    "mp4": "mp4", "m4v": "mp4", "mov": "mp4",
    "webm": "webm", "mkv": "mkv", "avi": "avi", "wmv": "wmv", "ts": "mpegts",
}  # fmt: skip


def _parse_container(stream: Stream) -> str:
    """Canonical container from the release filename extension (`behaviorHints.filename`),
    falling back to the `url` path tail. "" when absent/unknown (benefit of the doubt)."""
    hints = stream.get("behaviorHints")
    name = hints.get("filename") if isinstance(hints, dict) else None
    if not (isinstance(name, str) and "." in name):
        url = stream.get("url") or ""
        name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    if not name or "." not in name:
        return ""
    return _CONTAINER_BY_EXT.get(name.rsplit(".", 1)[1].lower(), "")


def _parse_audio(text: str) -> str:
    return next((name for name, pat in _AUDIO_PATTERNS if pat.search(text)), "")


def _parse_resolution(text: str) -> int:
    return next((res for pat, res in _RES_PATTERNS if pat.search(text)), 0)


def _parse_codec(text: str) -> str:
    if re.search(r"\bav1\b", text, re.I):
        return "av1"
    if re.search(r"x265|h\.?265|hevc", text, re.I):
        return "hevc"
    if re.search(r"x264|h\.?264|\bavc\b", text, re.I):
        return "h264"
    # Legacy tokens AFTER the modern ones: "MPEG-4 AVC" in a remux name must stay h264.
    # These codecs exist only in pre-2010 rips no receiver decodes (ADR 0017) — naming
    # them here (instead of "") denies them the unknown-codec benefit of the doubt.
    if re.search(r"xvid|divx|\bmp4v\b|\bmpe?g-?4\b", text, re.I):
        return "mpeg4"
    if re.search(r"\bmpe?g-?[12]\b", text, re.I):
        return "mpeg2"
    if re.search(r"\bvc-?1\b|\bwmv\b", text, re.I):
        return "vc1"
    return ""


def _parse_dv_profile(text: str) -> int | None:
    prof = re.search(r"\bDV[.\s]?P(\d)\b", text, re.I) or re.search(
        r"dolby.?vision.*?profile\s*(\d)", text, re.I
    )
    return int(prof.group(1)) if prof else None


def _parse_size_gb(text: str) -> float:
    if m := re.search(r"💾\s*([\d.]+)\s*GB", text, re.I):
        return float(m.group(1))
    if m := re.search(r"💾\s*([\d.]+)\s*MB", text, re.I):
        return float(m.group(1)) / 1024
    return 0.0


def _headline(value: object) -> str:
    """First line of a headline field, stripped ("" when absent)."""
    return str(value or "").split("\n", 1)[0].strip()


def _release_name(stream: Stream) -> str:
    """Release identity, structured first (ADR 0026): `behaviorHints.filename`, else the
    `description` headline, else the deprecated `title` headline. Empty only when the row
    carries none of the three — and an empty name silently disables both the wrong-title
    guard (`_title_matches`) and the cross-tracker dedup, so the chain must be exhausted."""
    return (
        _release_filename(stream).strip()
        or _headline(stream.get("description"))
        or _headline(stream.get("title"))
    )


def _size_gb(stream: Stream, text: str) -> float:
    """Release size, `behaviorHints.videoSize` first (ADR 0026).

    The structured value is an exact byte count of the VIDEO FILE; the text form is the
    torrent's total, which on a multi-file torrent also counts the extras. Binary scale
    (1024³) to stay on the same footing as `_parse_size_gb`, whose "MB" branch divides by
    1024 — so the numbers keep meaning the same thing as the ones already displayed.
    """
    hints = stream.get("behaviorHints")
    size = hints.get("videoSize") if isinstance(hints, dict) else None
    if isinstance(size, int | float) and size > 0:
        return float(size) / 1024**3
    return _parse_size_gb(text)


def _parse_seeders(text: str) -> int:
    return int(m.group(1)) if (m := re.search(r"👤\s*(\d+)", text)) else 0


# parse_stream is pure in (name, title, infoHash, fileIdx) and regex-heavy (~5 ms per
# stream); rank_streams runs up to 4×/play over the full result set, so unmemoized
# re-parsing cost ~0.8 s of CPU on a 50-stream title — seconds on a popular one
# (measured live 2026-07-13; closes audit 2026-06-09 finding #32). Process-lifetime
# cache, bounded by the streams seen in one run; StreamInfo is frozen, safe to share.
# LRU-bounded: a long TUI session parses every row of every title it browses.
_PARSE_CACHE: util.BoundedMemo[tuple, StreamInfo] = util.BoundedMemo(4096)


def parse_stream(stream: Stream) -> StreamInfo:
    hints = stream.get("behaviorHints")
    key = (
        stream.get("name") or "",
        stream.get("title") or "",
        # Every field the parsers now read must key the cache, or two distinct rows collapse
        # onto one StreamInfo (ADR 0026).
        stream.get("description") or "",
        (hints.get("videoSize") if isinstance(hints, dict) else None),
        stream.get("infoHash") or "",
        stream.get("fileIdx"),
        # The container comes from the filename/url, not name+title: key on it so two
        # releases differing only by container don't collide (ADR 0022).
        (hints.get("filename") if isinstance(hints, dict) else None) or stream.get("url") or "",
        # Two rows sharing a filename but differing in ready-url presence are NOT the same
        # StreamInfo: `has_url` decides whether the swarm-health filter applies.
        bool(stream.get("url")),
    )
    info = _PARSE_CACHE.get(key)
    if info is None:
        info = _parse_stream_uncached(stream)
        _PARSE_CACHE[key] = info
    return info


def _parse_stream_uncached(stream: Stream) -> StreamInfo:
    text = _text(stream)
    return StreamInfo(
        resolution=_parse_resolution(text),
        codec=_parse_codec(text),
        hdr=bool(re.search(r"\bhdr", text, re.I)),
        dv=bool(re.search(r"\bDV\b|dolby.?vision", text, re.I)),
        dv_profile=_parse_dv_profile(text),
        size_gb=_size_gb(stream, text),
        seeders=_parse_seeders(text),
        cached=bool(_CACHED_RE.search(stream.get("name") or "")),
        languages=_parse_languages(text),
        source=_parse_source(text),
        audio=_parse_audio(text),
        container=_parse_container(stream),
        release_name=_release_name(stream),
        info_hash=stream.get("infoHash") or "",
        file_idx=stream.get("fileIdx"),
        has_url=bool(stream.get("url")),
    )


# --- hardware capabilities (vainfo) --------------------------------------


@dataclass(frozen=True)
class HwCaps:
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


def detect_caps(*, use_cache: bool = True) -> HwCaps:
    """Detect HW decode capabilities via vainfo (cached). Conservative fallback."""
    path = _cache_path()
    if use_cache:
        data = util.load_json(path, {})
        with contextlib.suppress(KeyError, TypeError, ValueError):
            if data.get("version") == _CACHE_VERSION:
                return HwCaps(
                    codecs=frozenset(data["codecs"]),
                    max_resolution=int(data["max_resolution"]),
                    vaapi=bool(data["vaapi"]),
                )

    proc = util.run_cmd(["vainfo"], timeout=util.VAINFO_TIMEOUT)
    probed = _caps_from_vainfo(proc.stdout + proc.stderr) if proc else frozenset()
    # A real probe means VAAPI is usable; otherwise fall back conservatively and
    # mark vaapi unavailable so we don't force mpv onto a path we couldn't verify.
    vaapi = bool(probed)
    codecs = probed or frozenset(_FALLBACK_CODECS)

    caps = HwCaps(codecs=codecs, max_resolution=_DEFAULT_MAX_RESOLUTION, vaapi=vaapi)
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


def preferred_hwdec(caps: HwCaps) -> str | None:
    """The mpv hwdec method to force for this GPU, or None to leave mpv's choice.

    Returns ``vaapi`` when a real VAAPI probe succeeded (Intel/AMD): it's the mature
    HW path and avoids mpv probing experimental Vulkan decode (unsupported on many
    iGPUs) or a missing CUDA. NVIDIA-only setups aren't vainfo-detectable here, so we
    return None and let mpv decide. Verified live on Iris Xe: `vaapi` decodes zero-copy
    cleanly even under `gpu-api=vulkan`."""
    return "vaapi" if caps.vaapi else None


# Video codecs (ffprobe `codec_name`) the Default Media Receiver actually renders. The
# probe-time twin of `cast_caps().codecs` (name-guess): `stream_select` checks the REAL
# codec of a resolved stream against this before casting, because a release whose name
# tags no codec gets the benefit of the doubt in ranking — and an MPEG-4 ASP/DivX rip
# then "plays" as PLAYING + black screen with no receiver error (ADR 0017). AV1 is left
# out to match `cast_caps` (not guaranteed on older models).
CAST_VIDEO_DECODABLE = frozenset({"h264", "hevc", "vp8", "vp9"})

# Containers the Default Media Receiver can LOAD on a direct cast (ADR 0022). The DMR plays
# MP4/WebM/CMAF but REFUSES Matroska (.mkv): the LOAD is rejected at container-sniff —
# player_state UNKNOWN, receiver_error ERROR, content_id None (empirically confirmed on a
# Philips 43PUS9235, app CC1AD845) — even when the HEVC/AAC inside would decode fine. Unlike
# an undecodable video codec (→ mirror, ADR 0017), a bad container is fixed by the Tier-2
# copy/copy rewrap to MP4. WebM shares the "matroska,webm" ffprobe demuxer name, so the
# filename EXTENSION is authoritative for webm-vs-mkv.
CAST_CONTAINER_DECODABLE = frozenset({"mp4", "webm"})


def container_from_format(format_name: str, ext: str) -> str:
    """Normalize an ffprobe `format_name` to a canonical container token, using the parsed
    filename `ext` only to split the shared matroska/webm demuxer name. "" when unknown."""
    if not format_name:
        return ""
    fmt = format_name.lower()
    if fmt.startswith("mov,mp4") or "mp4" in fmt.split(","):
        return "mp4"
    if "matroska" in fmt or "webm" in fmt:
        return "webm" if ext == "webm" else "mkv"
    if "avi" in fmt:
        return "avi"
    if "mpegts" in fmt or fmt == "mpeg":
        return "mpegts"
    return ""


def container_castable(container: str) -> bool:
    """Whether the DMR can LOAD this container on a direct cast. Unknown ("") gets the
    benefit of the doubt, mirroring `_codec_supported("")`."""
    return not container or container in CAST_CONTAINER_DECODABLE


def container_mime(container: str) -> str:
    """MIME type to declare on a direct-cast LOAD (empty → let the receiver sniff)."""
    return {"mp4": "video/mp4", "webm": "video/webm"}.get(container, "")


def cast_caps() -> HwCaps:
    """Decode profile for a Chromecast/Google TV receiver — NOT the laptop GPU.

    Casting plays on the TV, so the laptop's vainfo codecs are irrelevant. A modern
    Google TV decodes H.264/HEVC(+10bit)/VP9 up to 4K; AV1 is not guaranteed on older
    models, so it's left out (such streams drop to the ⚠ section, still pickable)."""
    return HwCaps(
        codecs=frozenset({"h264", "hevc", "hevc10", "vp9"}),
        max_resolution=2160,
        vaapi=False,
    )


# --- support check + ranking ---------------------------------------------


def _codec_supported(codec: str, caps: HwCaps) -> bool:
    if not codec:
        return True  # unknown codec: give the benefit of the doubt (usually h264/hevc)
    if codec == "hevc":
        return "hevc" in caps.codecs or "hevc10" in caps.codecs
    return codec in caps.codecs


@dataclass(frozen=True)
class FilterSpec:
    """Stream-ranking knobs, grouped so they thread through `rank_streams`/
    `unsupported_reason` as one value instead of a dozen keyword args. Defaults are all
    no-ops (hardware-only ranking). Build one from a Config with `FilterSpec.from_config`."""

    max_resolution: int = 0
    allow_software: bool = False  # keep codecs the GPU can't decode
    allow_dv5: bool = False  # keep Dolby Vision Profile 5
    audio_langs: tuple[str, ...] = ()
    lang_filter: bool = False  # demote releases tagged only with non-preferred languages
    exclude_camrip: bool = False
    min_seeders: int = 0
    dedup: bool = False  # collapse duplicate releases across trackers
    cast_audio: bool = False  # demote audio a Chromecast can't decode (TrueHD/DTS/REMUX)
    # Tier-2 remux is enabled: Dolby/DTS audio is no longer a cast disqualifier (the host
    # remuxes it to AAC), so `cast_audio` only *ranks* (AAC preferred) instead of excluding.
    cast_remux: bool = False
    # Resolution cap applied ONLY to releases that need remuxing (a remux downloads the
    # whole file). 0 = no cap. A ranking preference, not an exclusion.
    cast_remux_max_resolution: int = 0
    # Size budget (GB) for a remux: a likely-remux release larger than this is demoted below
    # any feasible alternative, because the cast-time size guard would otherwise prompt/refuse
    # it. Size is the real download-cost proxy (the resolution cap can't see an unlabelled 4K
    # remux). 0 = no size demotion. Mirrors `Config.cast_remux_max_size_gb`.
    cast_remux_max_size: int = 0
    # Searched title, for the `title_match` demotion guard against a Torrentio mis-mapping
    # (an unrelated cached torrent out-ranking real releases). Empty = no-op ranking.
    title: str = ""
    # Hard-filter to this exact resolution (e.g. 1080). 0 = no exact filter. Distinct from
    # `max_resolution` (hardware safety ceiling): this is a per-session quality choice.
    exact_resolution: int = 0

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        *,
        cast_audio: bool = False,
        lang_filter: bool | None = None,
        title: str = "",
        exact_resolution: int = 0,
    ) -> FilterSpec:
        """Derive a spec from the user config. `cast_audio`/`lang_filter`/`exact_resolution`
        override per call (the cast path ranks against the receiver and ignores the language
        filter; quality choice is per-invocation via PlayOpts)."""
        return cls(
            max_resolution=cfg.max_resolution,
            allow_software=cfg.allow_software,
            allow_dv5=cfg.allow_dv5,
            audio_langs=tuple(cfg.audio_langs),
            lang_filter=cfg.lang_filter if lang_filter is None else lang_filter,
            exclude_camrip=cfg.exclude_camrip,
            min_seeders=cfg.min_seeders,
            dedup=cfg.dedup,
            cast_audio=cast_audio,
            cast_remux=cast_audio and cfg.cast_remux,
            cast_remux_max_resolution=cfg.cast_remux_max_resolution if cast_audio else 0,
            cast_remux_max_size=remux_size_budget(cfg) if cast_audio else 0,
            title=title,
            exact_resolution=exact_resolution,
        )


def remux_size_budget(cfg: Config) -> int:
    """GiB a cast remux may download: `cast_remux_max_size_gb`, tightened by the free space
    in the remux dir (with the 10% headroom `remux.remux_to_file` demands). Ranking against
    the disk keeps a pick the cast-time guard would refuse from winning (2026-10-01: a 66GB
    remux chosen with 52GB free fell back to a silent direct cast). 0 = unbounded."""
    free = util.free_gib(config_mod.remux_dir())  # 0.0 = unknown → don't tighten
    limits = [x for x in (cfg.cast_remux_max_size_gb, int(free / 1.1)) if x > 0]
    return min(limits) if limits else 0


def unsupported_reason(info: StreamInfo, caps: HwCaps, spec: FilterSpec) -> str | None:
    """Why this stream is excluded from the main list, or None if it belongs there.
    Order: hardware (codec/resolution/DV5) → exact quality → Cast audio → camrip → language
    → near-dead. The opt-in filters on `spec` default to no-ops, leaving the HW-only behaviour."""
    if spec.max_resolution and info.resolution > spec.max_resolution:
        return "8K" if info.resolution >= 4320 else f"{info.resolution}p"
    if not _codec_supported(info.codec, caps):
        return f"{info.codec.upper()} no-HW"
    if info.dv_profile == 5:
        return "Dolby Vision P5"
    # Per-session quality choice: keep only streams at the exact resolution (unknown = drop).
    if spec.exact_resolution and info.resolution != spec.exact_resolution:
        return f"{info.resolution}p" if info.resolution else "res ?"
    # Cast: the Default Media Receiver can't decode TrueHD/DTS/DTS-HD → silent audio.
    # A REMUX carries the lossless track even when the title omits the codec, so it's
    # demoted too; other unknown audio gets the benefit of the doubt (WEB-DLs rarely tag).
    # With Tier-2 remux enabled (`cast_remux`) these are castable (host remuxes audio to
    # AAC), so they're only ranked below native-AAC, not excluded.
    if spec.cast_audio and not spec.cast_remux:
        if info.audio in _CAST_LOSSLESS:
            return f"audio {info.audio.upper()}"
        if info.source == "remux":
            return "audio remux"
    if spec.exclude_camrip and info.source in _CAMRIP_SOURCES:
        return f"camrip ({info.source})"
    # Tagged with languages but none preferred (and not a multi-language release).
    if (
        spec.lang_filter
        and spec.audio_langs
        and info.languages
        and "multi" not in info.languages
        and not (info.languages & set(spec.audio_langs))
    ):
        return "lingua " + "/".join(sorted(info.languages))
    # Seeder count is a swarm-health heuristic, so it may only exclude rows the swarm will
    # actually have to serve. Two things exempt a row, and `has_url` is the stronger:
    # a ready debrid url is a FACT (the provider streams it, the swarm is never touched),
    # while the cached marker is a crowdsourced ESTIMATE whose glyph is addon-specific.
    # Resting the exemption on the estimate alone meant an addon whose dialect we failed to
    # parse had every row silently excluded as "pochi seeder" when it also omitted seeders.
    if spec.min_seeders and not (info.has_url or info.cached) and info.seeders < spec.min_seeders:
        return "pochi seeder"
    return None


_SEED_BUCKET = 40  # past this many seeders, treat as "well-seeded enough"

# Source quality rank for the score tiebreak (higher = better picture). Unknown source
# is neutral (above a webrip, below a webdl) so untagged web releases aren't punished.
_SOURCE_RANK = {
    "remux": 6,
    "bluray": 5,
    "webdl": 4,
    "webrip": 2,
    "hdtv": 1,
    "dvd": 1,
    "cam": 0,
    "ts": 0,
    "tc": 0,
    "scr": 0,
    "dcp": 0,
}
_SOURCE_UNKNOWN_RANK = 3


def _lang_rank(info: StreamInfo, audio_langs: tuple[str, ...]) -> int:
    """Audio-language preference for the score (higher = better):
      4 = name explicitly tags the primary language (audio_langs[0])
      3 = name explicitly tags another preferred (fallback) language
      2 = "multi"/"dual" only — likely multi-audio, but the name doesn't say which
          languages, so it's a *maybe*, not a match (ranks below an explicit hit)
      1 = untagged — the common case, benefit of the doubt
      0 = tagged only with non-preferred languages
    Neutral (1) when there's no preference or nothing parsed.

    Treating "multi"/"dual" as a maybe (2) rather than an explicit match is the fix for
    releases whose name only says "Dual": that token covers any language pair (e.g.
    Latino+Eng with no Italian at all), so it must not outrank a release that actually
    names the wanted language. The player confirms the real tracks with ffprobe before
    committing when the pick is only a maybe (multi/untagged)."""
    if not audio_langs or not info.languages:
        return 1
    if audio_langs[0] in info.languages:
        return 4
    if info.languages & set(audio_langs):
        return 3
    if "multi" in info.languages:
        return 2
    return 0


def _source_rank(source: str) -> int:
    return _SOURCE_RANK.get(source, _SOURCE_UNKNOWN_RANK)


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _title_guard(info: StreamInfo, title: str, audio_langs: tuple[str, ...]) -> bool:
    """`title_match` term. The searched title is the English catalog name, so two real
    releases would otherwise sink: a localized-title release ("Il.Grande.Gatsby.iTALiAN" vs
    "The Great Gatsby") — accepted when tagged with the primary audio language, the dub the
    user searched in — and every episode release, because callers pass the display title
    ("Fargo · S01E01 · Name", `labels.display_title`): only the part before " · " is matched."""
    if audio_langs and audio_langs[0] in info.languages:
        return True
    return _title_matches(info.release_name, title.split(" · ", 1)[0])


def _title_matches(release_name: str, title: str) -> bool:
    """Whether `release_name` plausibly belongs to the searched `title`. Guards against an
    unrelated torrent Torrentio maps under the wrong IMDb id (e.g. a "Charlie Brown" pack
    returned for "I.S.S.") that, being cached + untagged (resolution 0), would otherwise
    out-rank every real release. Used as a top-precedence **demotion** term, never an
    exclusion, so a false negative only sinks a legit release (still listed/pickable) rather
    than hiding it. No-op (True) when `title` is empty or too short to match reliably.

    Match if the compact (alphanumeric-only) title is a substring of the compact release
    name — this handles acronym titles like "I.S.S." (compact "iss") that have no usable
    word tokens — or, for multi-word titles, if at least half the title's ≥3-char word
    tokens appear in the release name (release names reorder/drop words freely)."""
    nt = _norm_title(title)
    nr = _norm_title(release_name)
    if len(nt) < 3 or not nr:
        return True
    if nt in nr:
        return True
    tokens = [t for t in re.split(r"[^a-z0-9]+", title.lower()) if len(t) >= 3]
    if not tokens:
        return False
    return sum(t in nr for t in tokens) / len(tokens) >= 0.5


def score_components(
    info: StreamInfo,
    audio_langs: tuple[str, ...] = (),
    *,
    cast: bool = False,
    cast_remux_cap: int = 0,
    cast_remux_size: int = 0,
    title: str = "",
) -> dict[str, float | int | bool]:
    """The labelled score terms in precedence order (highest first). `_score` is just the
    tuple of these values; `explain` renders the dict — keeping both here keeps the
    auto-pick and its explanation in sync.

    `title` (when set): a top-precedence `title_match` guard demoting a release whose name
    is unrelated to the searched title (a Torrentio mis-mapping) below every real one; a
    no-op (uniformly True) when `title` is empty, so default ranking is unchanged.

    Local (default): cached first (instant); then resolution; preferred audio language;
    better source (remux/bluray > web > …); HEVC over H264; "well-seeded enough"; finally
    the smaller file (faster start) among equals.

    Cast (`cast=True`): models the Default Media Receiver, not the TV's decoder. After
    cached and the remux size budget: the preferred audio language (the receiver can't
    switch tracks, so a wrong-language pick costs a remux later); then `direct_cast` — a
    release that plays as-is (decodable container, no remux-bound audio) beats one that
    needs a whole-file Tier-2 prepare; then audio the receiver decodes natively (AAC… over
    AC-3/E-AC-3). The receiver plays HEVC/4K/HDR natively, so resolution only ranks after
    these; H.264 is a tie-breaker (both decode here), not a constraint.

    `cast_remux_cap` (>0): among releases that need a host remux (Dolby/DTS), prefer those at
    or below this resolution — a remux downloads the whole file, so a 4K Dolby release is a
    huge fetch while a direct 4K cast is free. Ranked just after audio (a native-AAC release
    still wins) and before resolution (within the cap the best res still wins). A preference:
    a sole 4K Dolby release is still chosen.

    `cast_remux_size` (>0): `remux_within_size` demotes a likely-remux release bigger than this
    many GB below any feasible alternative — size is the real download cost, and it catches the
    case the resolution cap can't (an unlabelled 4K REMUX reads as decodable/unknown audio yet
    really carries lossless Dolby, so it would otherwise out-rank a modest option and then make
    the cast-time size guard prompt/refuse). Ranked right after `cached`: avoiding a pick the
    guard would reject matters more than codec/resolution. AAC releases never trip it (no remux,
    streamed directly even at 4K)."""
    if cast:
        needs_remux = _likely_needs_remux(info)
        within_remux_cap = (
            not needs_remux or cast_remux_cap == 0 or info.resolution <= cast_remux_cap
        )
        within_remux_size = (
            not needs_remux or cast_remux_size == 0 or info.size_gb <= cast_remux_size
        )
        return {
            "title_match": _title_guard(info, title, audio_langs),
            "cached": info.cached,
            "remux_within_size": within_remux_size,
            "lang": _lang_rank(info, audio_langs),
            "direct_cast": not needs_remux,
            "cast_audio": _cast_audio_rank(info),
            "remux_within_cap": within_remux_cap,
            "resolution": info.resolution,
            "source": _source_rank(info.source),
            "cast_h264": info.codec == "h264",
            "seeders": min(info.seeders, _SEED_BUCKET),
            "size": -info.size_gb,
        }
    return {
        "title_match": _title_guard(info, title, audio_langs),
        "cached": info.cached,
        "resolution": info.resolution,
        "lang": _lang_rank(info, audio_langs),
        "source": _source_rank(info.source),
        "hevc": info.codec == "hevc",
        "seeders": min(info.seeders, _SEED_BUCKET),
        "size": -info.size_gb,
    }


def _score(
    info: StreamInfo,
    audio_langs: tuple[str, ...] = (),
    *,
    cast: bool = False,
    cast_remux_cap: int = 0,
    cast_remux_size: int = 0,
    title: str = "",
) -> tuple:
    return tuple(
        score_components(
            info,
            audio_langs,
            cast=cast,
            cast_remux_cap=cast_remux_cap,
            cast_remux_size=cast_remux_size,
            title=title,
        ).values()
    )


def spec_components(info: StreamInfo, spec: FilterSpec) -> dict[str, float | int | bool]:
    """`score_components` under every knob of `spec` — the ranking key and what `--explain`
    shows. One call site for both, so the explanation can't drop a term the ranking used
    (it used to omit the remux size/cap budgets)."""
    return score_components(
        info,
        spec.audio_langs,
        cast=spec.cast_audio,
        cast_remux_cap=spec.cast_remux_max_resolution,
        cast_remux_size=spec.cast_remux_max_size,
        title=spec.title,
    )


@dataclass(frozen=True)
class RankedStream:
    stream: Stream
    info: StreamInfo
    reason: str | None = None  # set only for excluded streams


def _dedup_by_release(
    infos: list[tuple[Stream, StreamInfo]], spec: FilterSpec
) -> list[tuple[Stream, StreamInfo]]:
    """Collapse the same release seen on multiple trackers (identical release_name),
    keeping the copy the ranking itself would prefer (same `spec_components` key).
    Streams without a release_name are kept as-is."""
    best: dict[str, tuple[Stream, StreamInfo]] = {}
    out: list[tuple[Stream, StreamInfo]] = []
    for s, info in infos:
        key = util.release_key(info.release_name)
        if not key:
            out.append((s, info))
            continue
        cur = best.get(key)
        if cur is None or tuple(spec_components(info, spec).values()) > tuple(
            spec_components(cur[1], spec).values()
        ):
            best[key] = (s, info)
    out.extend(best.values())
    return out


def rank_streams(
    streams: list[Stream], caps: HwCaps, spec: FilterSpec
) -> tuple[list[RankedStream], list[RankedStream]]:
    """Split streams into (playable_sorted, excluded). `spec.allow_software` keeps codecs
    the GPU can't decode; `spec.allow_dv5` keeps Dolby Vision Profile 5. The opt-in filters
    (lang_filter/exclude_camrip/min_seeders) move non-matching streams to `excluded` with a
    reason; `spec.dedup` drops duplicate releases entirely (not in either list)."""
    infos = [(s, parse_stream(s)) for s in streams]
    if spec.dedup:
        infos = _dedup_by_release(infos, spec)
    playable: list[RankedStream] = []
    excluded: list[RankedStream] = []
    for s, info in infos:
        reason = unsupported_reason(info, caps, spec)
        if reason and spec.allow_software and reason.endswith("no-HW"):
            reason = None
        if reason and spec.allow_dv5 and reason.startswith("Dolby Vision"):
            reason = None
        if reason:
            excluded.append(RankedStream(s, info, reason))
        else:
            playable.append(RankedStream(s, info))
    playable.sort(key=lambda r: tuple(spec_components(r.info, spec).values()), reverse=True)
    return playable, excluded


def resolutions_of(playable: list[RankedStream]) -> list[int]:
    """Unique positive resolutions among ranked playable streams, highest first."""
    return sorted({r.info.resolution for r in playable if r.info.resolution > 0}, reverse=True)


# CLI / label aliases for --quality and the in-flow picker.
_QUALITY_ALIASES: dict[str, int] = {
    "auto": 0,
    "4k": 2160,
    "uhd": 2160,
    "2160": 2160,
    "2160p": 2160,
    "1440": 1440,
    "1440p": 1440,
    "2k": 1440,
    "1080": 1080,
    "1080p": 1080,
    "fhd": 1080,
    "720": 720,
    "720p": 720,
    "hd": 720,
    "480": 480,
    "480p": 480,
    "sd": 480,
}


def parse_quality(raw: str) -> int | None:
    """Parse a CLI/TUI quality token into resolution pixels (0 = auto), or None if invalid."""
    key = (raw or "").strip().lower()
    if not key:
        return None
    if key in _QUALITY_ALIASES:
        return _QUALITY_ALIASES[key]
    # Bare integer (optionally with trailing 'p'): 1080, 1080p, 2160.
    if key.endswith("p") and key[:-1].isdigit():
        key = key[:-1]
    if key.isdigit():
        return int(key)
    return None


def quality_label(res: int) -> str:
    """Human label for a quality choice: Auto, 2160p (4K), 1080p, …"""
    if res <= 0:
        return "Auto (migliore disponibile)"
    if res >= 4320:
        return f"{res}p (8K)"
    if res >= 2160:
        return f"{res}p (4K)"
    if res == 1080:
        return "1080p (Full HD)"
    if res == 720:
        return "720p (HD)"
    return f"{res}p"
