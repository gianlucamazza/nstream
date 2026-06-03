"""Configuration loading and shared payload types."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict


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
    # Autoplay the next episode of a series via the in-video overlay.
    autoplay: bool = True
    # Seconds before the end of an episode at which the overlay appears.
    autoplay_lead: int = 15
    # Quiet mpv's terminal output (hide the track list and decoder/driver warnings,
    # keep the progress line and errors). Injected only if you haven't set msg-level.
    mpv_quiet: bool = True
    mpv_args: list[str] = field(default_factory=list)


def config_path() -> Path:
    """Resolve the config path, honouring XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "nstream" / "config.json"


def state_path() -> Path:
    """Resolve the watch-history path, honouring XDG_STATE_HOME."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "nstream" / "history.json"


def load() -> Config:
    path = config_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config mancante: {path}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"config non valido ({path}): {e}") from e

    base = raw.get("torrentio_base")
    if not base:
        raise ConfigError(f"'torrentio_base' assente in {path}")
    # Absent → default; explicit "", false or null → disabled.
    hwdec_raw = raw.get("hwdec", Config.hwdec)
    hwdec = str(hwdec_raw) if hwdec_raw else ""
    try:
        autoplay_lead = int(raw.get("autoplay_lead", Config.autoplay_lead))
    except (TypeError, ValueError):
        autoplay_lead = Config.autoplay_lead
    # Clamp to a sane range: 0 would show the overlay only in the last half second,
    # huge values would keep it on screen the whole time.
    autoplay_lead = max(1, min(autoplay_lead, 120))
    return Config(
        torrentio_base=base,
        cinemeta=raw.get("cinemeta", Config.cinemeta),
        opensubtitles=raw.get("opensubtitles", Config.opensubtitles),
        subtitle_langs=list(raw.get("subtitle_langs", ["ita", "eng"])),
        audio_langs=list(raw.get("audio_langs", ["ita", "eng"])),
        addons=list(raw.get("addons", [])),
        history_enabled=bool(raw.get("history_enabled", True)),
        hwdec=hwdec,
        autoplay=bool(raw.get("autoplay", Config.autoplay)),
        autoplay_lead=autoplay_lead,
        mpv_quiet=bool(raw.get("mpv_quiet", Config.mpv_quiet)),
        mpv_args=list(raw.get("mpv_args", [])),
    )


def load_raw() -> dict:
    """Return the raw config dict, or {} if the file is missing/corrupt."""
    try:
        data = json.loads(config_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(updates: dict) -> None:
    """Merge `updates` into the on-disk config and rewrite it atomically (0600),
    preserving keys nstream doesn't model. The file holds the RD token."""
    data = load_raw()
    data.update(updates)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
