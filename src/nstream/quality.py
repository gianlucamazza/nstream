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

from . import languages, util
from .config import Config, Stream

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


# Release-name language tokens → ISO code, derived from the single language registry.
# Word-boundary matched so a group name like "-CYBER" or "ENG" inside another word doesn't
# false-positive.
_LANG_TOKENS = {lang.code: lang.tokens for lang in languages.LANGUAGES}
_LANG_RE = {
    code: re.compile(r"(?<![A-Za-z])(?:" + "|".join(toks) + r")(?![A-Za-z])", re.I)
    for code, toks in _LANG_TOKENS.items()
}
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


def _parse_resolution(text: str) -> int:
    return next((res for pat, res in _RES_PATTERNS if pat.search(text)), 0)


def _parse_codec(text: str) -> str:
    if re.search(r"\bav1\b", text, re.I):
        return "av1"
    if re.search(r"x265|h\.?265|hevc", text, re.I):
        return "hevc"
    if re.search(r"x264|h\.?264|\bavc\b", text, re.I):
        return "h264"
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


def _parse_seeders(text: str) -> int:
    return int(m.group(1)) if (m := re.search(r"👤\s*(\d+)", text)) else 0


def parse_stream(stream: Stream) -> StreamInfo:
    text = _text(stream)
    return StreamInfo(
        resolution=_parse_resolution(text),
        codec=_parse_codec(text),
        hdr=bool(re.search(r"\bhdr", text, re.I)),
        dv=bool(re.search(r"\bDV\b|dolby.?vision", text, re.I)),
        dv_profile=_parse_dv_profile(text),
        size_gb=_parse_size_gb(text),
        seeders=_parse_seeders(text),
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
        data = util.load_json(path, {})
        with contextlib.suppress(KeyError, TypeError, ValueError):
            if data.get("version") == _CACHE_VERSION:
                return Caps(
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

    @classmethod
    def from_config(
        cls, cfg: Config, *, cast_audio: bool = False, lang_filter: bool | None = None
    ) -> FilterSpec:
        """Derive a spec from the user config. `cast_audio`/`lang_filter` override per call
        (the cast path ranks against the receiver and ignores the language filter)."""
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
        )


def unsupported_reason(info: StreamInfo, caps: Caps, spec: FilterSpec) -> str | None:
    """Why this stream is excluded from the main list, or None if it belongs there.
    Order: hardware (codec/resolution/DV5) → Cast audio → camrip → language → near-dead.
    The opt-in filters on `spec` default to no-ops, leaving the HW-only behaviour."""
    if spec.max_resolution and info.resolution > spec.max_resolution:
        return "8K" if info.resolution >= 4320 else f"{info.resolution}p"
    if not _codec_supported(info.codec, caps):
        return f"{info.codec.upper()} no-HW"
    if info.dv_profile == 5:
        return "Dolby Vision P5"
    # Cast: the Default Media Receiver can't decode TrueHD/DTS/DTS-HD → silent audio.
    # A REMUX carries the lossless track even when the title omits the codec, so it's
    # demoted too; other unknown audio gets the benefit of the doubt (WEB-DLs rarely tag).
    if spec.cast_audio:
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
    # Non-cached torrent with too few seeders may never start (cached [RD+] are exempt).
    if spec.min_seeders and not info.cached and info.seeders < spec.min_seeders:
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
    """Audio-language preference for the score: 2 = tagged with a preferred language (or
    multi-audio, which usually carries it), 1 = untagged (the common case — benefit of the
    doubt), 0 = tagged only with non-preferred languages. Neutral (1) with no preference.

    This makes the auto-pick favour a file that actually contains the wanted audio track
    (mpv then selects it via --alang); without it an untagged release could win on quality
    and leave mpv with no matching track."""
    if not audio_langs or not info.languages:
        return 1
    if "multi" in info.languages or info.languages & set(audio_langs):
        return 2
    return 0


def _source_rank(source: str) -> int:
    return _SOURCE_RANK.get(source, _SOURCE_UNKNOWN_RANK)


def score_components(
    info: StreamInfo, audio_langs: tuple[str, ...] = ()
) -> dict[str, float | int | bool]:
    """The labelled score terms in precedence order (highest first). `_score` is just the
    tuple of these values; `explain` renders the dict — keeping both here keeps the
    auto-pick and its explanation in sync.

    cached first (instant); then resolution; preferred audio language; better source
    (remux/bluray > web > …); HEVC over H264; "well-seeded enough" (bucketed so popularity
    doesn't force a huge file); finally the smaller file (faster start) among equals."""
    return {
        "cached": info.cached,
        "resolution": info.resolution,
        "lang": _lang_rank(info, audio_langs),
        "source": _source_rank(info.source),
        "hevc": info.codec == "hevc",
        "seeders": min(info.seeders, _SEED_BUCKET),
        "size": -info.size_gb,
    }


def _score(info: StreamInfo, audio_langs: tuple[str, ...] = ()) -> tuple:
    return tuple(score_components(info, audio_langs).values())


@dataclass(frozen=True)
class RankedStream:
    stream: Stream
    info: StreamInfo
    reason: str | None = None  # set only for excluded streams


def _dedup_by_release(
    infos: list[tuple[Stream, StreamInfo]], audio_langs: tuple[str, ...]
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
        if cur is None or _score(info, audio_langs) > _score(cur[1], audio_langs):
            best[key] = (s, info)
    out.extend(best.values())
    return out


def rank_streams(
    streams: list[Stream], caps: Caps, spec: FilterSpec
) -> tuple[list[RankedStream], list[RankedStream]]:
    """Split streams into (playable_sorted, excluded). `spec.allow_software` keeps codecs
    the GPU can't decode; `spec.allow_dv5` keeps Dolby Vision Profile 5. The opt-in filters
    (lang_filter/exclude_camrip/min_seeders) move non-matching streams to `excluded` with a
    reason; `spec.dedup` drops duplicate releases entirely (not in either list)."""
    infos = [(s, parse_stream(s)) for s in streams]
    if spec.dedup:
        infos = _dedup_by_release(infos, spec.audio_langs)
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
    playable.sort(key=lambda r: _score(r.info, spec.audio_langs), reverse=True)
    return playable, excluded
