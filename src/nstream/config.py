"""Configuration loading and shared payload types."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from . import util


class ConfigError(Exception):
    """Raised when the config file is missing or malformed."""


class Meta(TypedDict, total=False):
    """A Cinemeta catalog/meta entry. Catalog responses carry the first four fields;
    the full meta endpoint adds the rest (surfaced in the preview pane)."""

    id: str
    type: str
    name: str
    releaseInfo: str
    poster: str
    background: str
    description: str
    imdbRating: str
    genres: list[str]
    runtime: str
    cast: list[str]
    director: list[str]
    released: str


class Video(TypedDict, total=False):
    """A Cinemeta series episode."""

    id: str
    season: int
    episode: int
    name: str
    overview: str
    thumbnail: str
    released: str


class Stream(TypedDict, total=False):
    """A Torrentio stream result. Debrid/cached results carry a ready HTTP `url`;
    pure-torrent results (debrid off) carry `infoHash` (+ optional `fileIdx`/`sources`)
    instead, resolved to a local HTTP url by the P2P engine before playback."""

    name: str
    title: str
    url: str
    infoHash: str
    fileIdx: int
    sources: list[str]
    behaviorHints: (
        dict  # Torrentio extra (e.g. {"filename": "..."}) — used for hybrid/native file match
    )


class Subtitle(TypedDict, total=False):
    """An OpenSubtitles v3 subtitle track."""

    id: str
    url: str
    lang: str


class HistoryEntry(TypedDict, total=False):
    """A persisted watch record used for resume and continue-watching."""

    video_id: str
    title: str
    type: str
    series_id: str
    season: int
    episode: int
    position: float
    duration: float
    ts: float


@dataclass(frozen=True)
class PlayOpts:
    """Per-invocation playback preferences threaded through the flow."""

    auto: bool  # auto-pick the top stream (skip the stream menu)
    cast: bool  # send playback to a Chromecast (catt) instead of mpv
    sub_mode: str | None  # None = no subs, "auto" = pick preferred lang, "menu" = fzf
    sub_lang: str | None  # force this language for sub_mode="auto"
    history: bool  # record/resume watch history
    autoplay: bool  # offer the next-episode overlay for series
    cast_choose: bool = False  # force the device picker (explicit "cast this" action)
    audio_lang: str | None = None  # force this audio/dub language (headless --audio-lang)


@dataclass(frozen=True)
class Config:
    # Torrentio config string. With a debrid segment (`…|realdebrid=TOKEN`) Torrentio
    # returns ready debrid urls; without one it returns pure-torrent streams that the
    # local P2P engine resolves. Default is token-less so local playback works out of box.
    torrentio_base: str = "sort=qualitysize"
    cinemeta: str = "https://v3-cinemeta.strem.io"
    opensubtitles: str = "https://opensubtitles-v3.strem.io"
    subtitle_langs: list[str] = field(default_factory=lambda: ["ita", "eng"])
    # Preferred audio languages for mpv track auto-selection (--alang), in order.
    audio_langs: list[str] = field(default_factory=lambda: ["ita", "eng"])
    # Primary (native) language. Drives audio selection and the safety-subtitle logic
    # (subtitles auto-on when the actual audio isn't this language). Empty = first of
    # audio_langs; the remaining audio_langs are acceptable fallbacks.
    primary_lang: str = ""
    # Extra Stremio addon manifest URLs (beyond the built-in Cinemeta/Torrentio/
    # OpenSubtitles), aggregated for streams/subtitles/catalogs.
    addons: list[str] = field(default_factory=list)
    history_enabled: bool = True
    # mpv hardware decoding, injected only if the user hasn't set hwdec themselves
    # (in mpv.conf or mpv_args). Empty string disables the injection.
    hwdec: str = "auto-safe"
    # Default playback mode: Enter on a title plays the best stream immediately
    # (no stream/track menu); Tab in the list flips to manual for that pick. Off
    # makes manual the default and Tab the auto shortcut.
    auto_play: bool = True
    # Send playback to a Chromecast (via `catt`) instead of mpv by default.
    # --cast / --local override per session. cast_device pins a device name for
    # `catt -d`; empty = resolve per-LAN via a fresh `catt scan` (see caster.py).
    prefer_cast: bool = False
    cast_device: str = ""
    # Autoplay the next episode of a series via the in-video overlay.
    autoplay: bool = True
    # Seconds before the end of an episode at which the overlay appears.
    autoplay_lead: int = 15
    # Quiet mpv's terminal output (hide the track list and decoder/driver warnings,
    # keep the progress line and errors). Injected only if you haven't set msg-level.
    mpv_quiet: bool = True
    # Hardware-aware stream ranking: auto-pick the best stream the GPU can actually
    # play, excluding e.g. 8K and Dolby Vision P5 (and codecs the GPU can't decode).
    hw_filter: bool = True
    max_resolution: int = 2160  # 0 = no cap
    allow_software: bool = False  # keep streams whose codec has no HW decode
    allow_dv5: bool = False  # keep Dolby Vision Profile 5 streams
    # Stream-list curation (Torrentio returns ~150/title): keep only preferred-language
    # or untagged releases in the main list, drop camrips / near-dead torrents / dupes,
    # and cap how many are shown (a "show all" entry expands the rest).
    lang_filter: bool = True  # demote releases tagged only with non-preferred languages
    exclude_camrip: bool = True  # CAM/TS/TC/SCR out of the main list
    min_seeders: int = 3  # non-cached torrents below this are near-dead (0 = off)
    dedup: bool = True  # collapse the same release across trackers
    max_streams: int = 20  # cap the manual menu (0 = no cap)
    mpv_args: list[str] = field(default_factory=list)
    # TUI appearance. nerd_font: "auto" (env opt-in) | "on" | "off"; posters: render
    # poster thumbnails in the fzf preview pane (needs chafa); image_mode: "auto" |
    # "off" to force the image protocol off regardless of terminal.
    nerd_font: str = "auto"
    posters: bool = True
    image_mode: str = "auto"
    # Playback backend: "local" streams torrents peer-to-peer through a TorrServer
    # instance nstream drives (free, default); "debrid" plays the ready urls Torrentio
    # returns for a configured debrid provider; "auto" is hybrid — prefer cached debrid
    # urls and fall back to local P2P (merges both Torrentio queries by filename);
    # "native" discovers pure-torrent streams (token-less Torrentio) and resolves the
    # chosen one through the provider's own API (TorBox/Premiumize — see debrid.py),
    # falling back to local P2P on failure. Pure-torrent streams always go local.
    playback_backend: str = "local"
    engine_port: int = 8090  # TorrServer HTTP port (also the one nstream spawns)
    engine_cache_mb: int = 256  # TorrServer in-memory read-ahead cache
    engine_download_dir: str = ""  # torrent data dir; "" → $XDG_CACHE_HOME/nstream/torrents
    p2p_ack: bool = False  # user acknowledged the P2P privacy notice (IP exposed to peers)
    # Block local P2P streaming unless a VPN interface is detected (default off: only warn).
    p2p_require_vpn: bool = False

    @property
    def primary(self) -> str:
        """Primary/native language code: explicit `primary_lang`, else first audio lang."""
        return self.primary_lang or (self.audio_langs[0] if self.audio_langs else "")

    @property
    def fallback_langs(self) -> list[str]:
        """Acceptable non-primary audio languages, in preference order."""
        return [code for code in self.audio_langs if code != self.primary]


# Debrid provider keys Torrentio understands, embedded in `torrentio_base` as `key=token`.
# Single source of truth shared by settings (provider picker) and addons (strip for local
# backend); kept provider-agnostic — code never special-cases an individual provider.
# Single source of truth (key → display name); both the provider key set and the settings
# picker derive from it, and log.py builds its redaction regex from the keys — no drift.
DEBRID_PROVIDER_NAMES: dict[str, str] = {
    "realdebrid": "RealDebrid",
    "alldebrid": "AllDebrid",
    "premiumize": "Premiumize",
    "torbox": "TorBox",
    "debridlink": "Debrid-Link",
    "easydebrid": "EasyDebrid",
    "offcloud": "Offcloud",
    "putio": "Put.io",
}
DEBRID_PROVIDERS: tuple[str, ...] = tuple(DEBRID_PROVIDER_NAMES)


# Allowed values for the enum-like string config fields (bad values fall back to default).
_ENUM_VALUES: dict[str, set[str]] = {
    "nerd_font": {"auto", "on", "off"},
    "image_mode": {"auto", "off"},
    "playback_backend": {"local", "debrid", "auto", "native"},
}


def debrid_credentials(base: str) -> tuple[str, str] | None:
    """Extract the (provider, token) pair from a Torrentio config string, or None when no
    debrid segment is present. Single source of truth for the token — the native resolver
    reads it from here too (sent as an Authorization header), so there's no second copy to
    keep in sync. Only the first debrid segment counts (Torrentio expects one)."""
    for seg in base.split("|"):
        key, sep, val = seg.partition("=")
        if sep and key in DEBRID_PROVIDERS and val:
            return (key, val)
    return None


def _enum_str(raw: dict, key: str, default: str) -> str:
    val = str(raw.get(key, default))
    return val if val in _ENUM_VALUES[key] else default


# (min, max) bounds for the integer config fields, shared with the settings editor so
# validation lives in one place. max_resolution's 0 means "no cap"; 4320 is 8K.
INT_BOUNDS: dict[str, tuple[int, int]] = {
    "autoplay_lead": (1, 120),
    "max_resolution": (0, 4320),
    "min_seeders": (0, 100),
    "max_streams": (0, 500),
    "engine_port": (1024, 65535),
    "engine_cache_mb": (32, 4096),
}


def config_path() -> Path:
    """Resolve the config path, honouring XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "nstream" / "config.json"


def state_path() -> Path:
    """Resolve the watch-history path, honouring XDG_STATE_HOME."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "nstream" / "history.json"


def _bounded_int(raw: dict, key: str, default: int) -> int:
    """Coerce a config int, falling back to `default` on a bad value and clamping to the
    field's INT_BOUNDS range (so out-of-range values can't break ranking/overlay)."""
    lo, hi = INT_BOUNDS[key]
    try:
        return max(lo, min(int(raw.get(key, default)), hi))
    except (TypeError, ValueError):
        return default


def load() -> Config:
    path = config_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config mancante: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"config non valido ({path}): {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config non valido ({path}): atteso un oggetto JSON")

    # Token-less default so a fresh config still streams locally; a debrid segment is
    # added to torrentio_base only when the user opts into a paid provider.
    base = raw.get("torrentio_base") or Config.torrentio_base
    # Absent → default; explicit "", false or null → disabled.
    hwdec_raw = raw.get("hwdec", Config.hwdec)
    hwdec = str(hwdec_raw) if hwdec_raw else ""
    return Config(
        torrentio_base=base,
        cinemeta=raw.get("cinemeta", Config.cinemeta),
        opensubtitles=raw.get("opensubtitles", Config.opensubtitles),
        subtitle_langs=list(raw.get("subtitle_langs", ["ita", "eng"])),
        audio_langs=list(raw.get("audio_langs", ["ita", "eng"])),
        primary_lang=str(raw.get("primary_lang", "") or ""),
        addons=list(raw.get("addons", [])),
        history_enabled=bool(raw.get("history_enabled", True)),
        hwdec=hwdec,
        auto_play=bool(raw.get("auto_play", Config.auto_play)),
        prefer_cast=bool(raw.get("prefer_cast", Config.prefer_cast)),
        cast_device=str(raw.get("cast_device", Config.cast_device) or ""),
        autoplay=bool(raw.get("autoplay", Config.autoplay)),
        autoplay_lead=_bounded_int(raw, "autoplay_lead", Config.autoplay_lead),
        mpv_quiet=bool(raw.get("mpv_quiet", Config.mpv_quiet)),
        hw_filter=bool(raw.get("hw_filter", Config.hw_filter)),
        max_resolution=_bounded_int(raw, "max_resolution", Config.max_resolution),
        allow_software=bool(raw.get("allow_software", Config.allow_software)),
        allow_dv5=bool(raw.get("allow_dv5", Config.allow_dv5)),
        lang_filter=bool(raw.get("lang_filter", Config.lang_filter)),
        exclude_camrip=bool(raw.get("exclude_camrip", Config.exclude_camrip)),
        min_seeders=_bounded_int(raw, "min_seeders", Config.min_seeders),
        dedup=bool(raw.get("dedup", Config.dedup)),
        max_streams=_bounded_int(raw, "max_streams", Config.max_streams),
        mpv_args=list(raw.get("mpv_args", [])),
        nerd_font=_enum_str(raw, "nerd_font", Config.nerd_font),
        posters=bool(raw.get("posters", Config.posters)),
        image_mode=_enum_str(raw, "image_mode", Config.image_mode),
        playback_backend=_enum_str(raw, "playback_backend", Config.playback_backend),
        engine_port=_bounded_int(raw, "engine_port", Config.engine_port),
        engine_cache_mb=_bounded_int(raw, "engine_cache_mb", Config.engine_cache_mb),
        engine_download_dir=str(raw.get("engine_download_dir", Config.engine_download_dir) or ""),
        p2p_ack=bool(raw.get("p2p_ack", Config.p2p_ack)),
        p2p_require_vpn=bool(raw.get("p2p_require_vpn", Config.p2p_require_vpn)),
    )


def load_raw() -> dict:
    """Return the raw config dict, or {} if the file is missing/corrupt."""
    return util.load_json(config_path(), {})


def save(updates: dict) -> None:
    """Merge `updates` into the on-disk config and rewrite it atomically (0600),
    preserving keys nstream doesn't model. The file holds the RD token."""
    data = load_raw()
    data.update(updates)
    util.atomic_write(
        config_path(),
        lambda f: json.dump(data, f, ensure_ascii=False, indent=2),
        prefix=".config-",
    )
