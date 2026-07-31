"""Domain payload shapes shared across modules.

Stremio/Cinemeta/OpenSubtitles wire types and watch-history records live here so
`config` owns only schema I/O + `Config`/`PlayOpts`. Import types from
`nstream.types`, not from config.
"""

from __future__ import annotations

from typing import TypedDict


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
    """A stream-addon result. Debrid/cached rows carry a ready HTTP `url`; pure-torrent
    rows carry `infoHash` (+ optional `fileIdx`/`sources`) resolved to a local HTTP url
    by the P2P engine before playback. `addon` is set by `api.streams` (provenance)."""

    name: str
    title: str
    url: str
    infoHash: str
    fileIdx: int
    sources: list[str]
    behaviorHints: (
        dict  # Torrentio/Comet extra (e.g. {"filename": "..."}) — hybrid/native file match
    )
    addon: str  # stream-addon display name (Torrentio, Comet, …) — set by api.streams


class Subtitle(TypedDict, total=False):
    """An OpenSubtitles v3 subtitle track."""

    id: str
    url: str
    lang: str
    # Set by `api.subtitles` on tracks returned by the videoHash query: timed for the
    # exact file being played (OSHash match) → preferred within a language (ADR 0018).
    hash_match: bool


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
