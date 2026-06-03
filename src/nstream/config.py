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
    """A Cinemeta catalog/meta entry."""

    id: str
    type: str
    name: str
    releaseInfo: str


class Video(TypedDict, total=False):
    """A Cinemeta series episode."""

    id: str
    season: int
    episode: int
    name: str


class Stream(TypedDict, total=False):
    """A Torrentio stream result."""

    name: str
    title: str
    url: str


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
class Config:
    torrentio_base: str
    cinemeta: str = "https://v3-cinemeta.strem.io"
    opensubtitles: str = "https://opensubtitles-v3.strem.io"
    subtitle_langs: list[str] = field(default_factory=lambda: ["ita", "eng"])
    # Preferred audio languages for mpv track auto-selection (--alang), in order.
    audio_langs: list[str] = field(default_factory=lambda: ["ita", "eng"])
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
    # `catt -d`; empty = resolve per-LAN via `cast-resolve`, then catt's default.
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


# (min, max) bounds for the integer config fields, shared with the settings editor so
# validation lives in one place. max_resolution's 0 means "no cap"; 4320 is 8K.
INT_BOUNDS: dict[str, tuple[int, int]] = {
    "autoplay_lead": (1, 120),
    "max_resolution": (0, 4320),
    "min_seeders": (0, 100),
    "max_streams": (0, 500),
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

    base = raw.get("torrentio_base")
    if not base:
        raise ConfigError(f"'torrentio_base' assente in {path}")
    # Absent → default; explicit "", false or null → disabled.
    hwdec_raw = raw.get("hwdec", Config.hwdec)
    hwdec = str(hwdec_raw) if hwdec_raw else ""
    return Config(
        torrentio_base=base,
        cinemeta=raw.get("cinemeta", Config.cinemeta),
        opensubtitles=raw.get("opensubtitles", Config.opensubtitles),
        subtitle_langs=list(raw.get("subtitle_langs", ["ita", "eng"])),
        audio_langs=list(raw.get("audio_langs", ["ita", "eng"])),
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
