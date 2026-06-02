"""Configuration loading and shared payload types."""

from __future__ import annotations

import json
import os
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
    history_enabled: bool = True
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
    return Config(
        torrentio_base=base,
        cinemeta=raw.get("cinemeta", Config.cinemeta),
        opensubtitles=raw.get("opensubtitles", Config.opensubtitles),
        subtitle_langs=list(raw.get("subtitle_langs", ["ita", "eng"])),
        history_enabled=bool(raw.get("history_enabled", True)),
        mpv_args=list(raw.get("mpv_args", [])),
    )
