"""Watch-history persistence for resume and continue-watching.

State lives in ``XDG_STATE_HOME/nstream/history.json`` as a mapping
``video_id -> HistoryEntry``. All reads are best-effort: a missing or corrupt
file yields an empty history so playback is never blocked by state errors.
"""

from __future__ import annotations

import json
import time

from . import util
from .config import Config, HistoryEntry, Video, state_path

# Past this fraction of the runtime a title counts as watched and drops out of
# the continue-watching list.
WATCHED_THRESHOLD = 0.9


def make_entry(
    video_id: str,
    title: str,
    typ: str,
    pos: float,
    dur: float,
    *,
    series_id: str = "",
    video: Video | None = None,
    season: int = 0,
    episode: int = 0,
) -> HistoryEntry:
    """Build a HistoryEntry, filling series fields from `video` when given."""
    entry: HistoryEntry = {
        "video_id": video_id,
        "title": title,
        "type": typ,
        "position": pos,
        "duration": dur,
        "ts": time.time(),
    }
    if typ == "series":
        entry["series_id"] = series_id
        entry["season"] = video.get("season", 0) if video is not None else season
        entry["episode"] = video.get("episode", 0) if video is not None else episode
    return entry


def load_history(cfg: Config) -> dict[str, HistoryEntry]:
    if not cfg.history_enabled:
        return {}
    return util.load_json(state_path(), {})


# Within this many seconds of the end a title counts as finished, even if the
# fraction is below WATCHED_THRESHOLD (e.g. padded duration / long credits, or
# mpv paused at EOF with keep-open=yes).
END_TAIL_SECONDS = 60.0


def _watched(entry: HistoryEntry) -> bool:
    duration = entry.get("duration") or 0.0
    if duration <= 0:
        return False
    position = entry.get("position") or 0.0
    tail = min(END_TAIL_SECONDS, 0.05 * duration)  # relative, so short clips aren't mislabelled
    return position / duration > WATCHED_THRESHOLD or position >= duration - tail


def resume_position(cfg: Config, video_id: str) -> float | None:
    """The position to resume from, or None if there's no usable resume point
    (no history entry, or the title is effectively finished — so we never restart
    at the very end when mpv was left paused at EOF with keep-open)."""
    entry = load_history(cfg).get(video_id)
    if not entry or _watched(entry):
        return None
    start = entry.get("position")
    dur = entry.get("duration") or 0.0
    if start and dur > 0:
        return min(start, dur - 5)
    return start


def save_entry(cfg: Config, entry: HistoryEntry) -> None:
    if not cfg.history_enabled:
        return
    history = load_history(cfg)
    vid = entry.get("video_id")
    if not vid:
        return
    if _watched(entry):
        history.pop(vid, None)
    else:
        history[vid] = entry

    util.atomic_write(
        state_path(),
        lambda f: json.dump(history, f, ensure_ascii=False),
        prefix=".history-",
    )


def recent(cfg: Config, limit: int = 30, typ: str | None = None) -> list[HistoryEntry]:
    entries = [e for e in load_history(cfg).values() if not _watched(e)]
    if typ is not None:
        # Legacy entries without "type" predate series support → treat as "movie"
        # (same default the CLI uses when reading entry types).
        entries = [e for e in entries if e.get("type", "movie") == typ]
    entries.sort(key=lambda e: e.get("ts", 0.0), reverse=True)
    return entries[:limit]
