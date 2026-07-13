"""Watch-history persistence for resume and continue-watching.

State lives in ``XDG_STATE_HOME/nstream/history.json`` as a mapping
``video_id -> HistoryEntry``. All reads are best-effort: a missing or corrupt
file yields an empty history so playback is never blocked by state errors.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator
from typing import cast

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


@contextlib.contextmanager
def _history_lock() -> Iterator[None]:
    """Serialise the save_entry read-modify-write across processes (a headless `--follow`
    can end while an interactive session saves another title; without the lock the last
    atomic_write silently drops the other writer's entry). Best-effort like all state I/O:
    if the lock file can't be opened, proceed unlocked rather than block playback."""
    try:
        fd = os.open(state_path().with_name(".history.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        yield
        return
    try:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def save_entry(cfg: Config, entry: HistoryEntry) -> None:
    if not cfg.history_enabled:
        return
    vid = entry.get("video_id")
    if not vid:
        return
    with contextlib.suppress(OSError):  # atomic_write recreates it (and surfaces the error)
        state_path().parent.mkdir(parents=True, exist_ok=True)
    with _history_lock():
        history = load_history(cfg)
        if _watched(entry):
            history.pop(vid, None)
        else:
            history[vid] = entry

        util.atomic_write(
            state_path(),
            lambda f: json.dump(history, f, ensure_ascii=False),
            prefix=".history-",
        )


def note_started(cfg: Config, entry: HistoryEntry) -> None:
    """Record that playback of `entry` started when the end position can't be known
    (headless fire-and-return cast: no poll loop runs). Keeps a previously known
    duration so the watched/near-end logic stays meaningful; the real position lands
    later via `update_from_receiver` (`--stop`/`--status`)."""
    prev = load_history(cfg).get(entry.get("video_id", ""))
    if prev and not entry.get("duration"):
        entry["duration"] = prev.get("duration", 0.0)
    save_entry(cfg, entry)


# RunState slot for the receiver-side session of a fire-and-return cast: which entry is
# on the TV, so a later `--stop`/`--status` can attribute the receiver's position to it.
CAST_SESSION = "watch"


def remember_cast(cfg: Config, entry: HistoryEntry, device: str | None) -> None:
    """Persist the fire-and-return cast session (entry + device) across nstream runs.
    Interactive and `--follow` casts don't need this — their poll loop saves directly."""
    if not cfg.history_enabled:
        return
    util.RunState(CAST_SESSION).write({**entry, "device": device or ""})


def update_from_receiver(
    cfg: Config, device: str | None, position: float, duration: float, *, clear: bool = False
) -> bool:
    """Merge a receiver-reported position into the session entry saved by `remember_cast`,
    if one exists for `device`. Returns True when an entry was persisted. `clear` drops
    the session file afterwards (the `--stop` one-shot); a zero/idle position still clears
    but persists nothing."""
    run_state = util.RunState(CAST_SESSION)
    session = run_state.read()
    if not session:
        return False
    if device and session.get("device") and session["device"] != device:
        return False
    if clear:
        run_state.clear()
    if not (position > 0 and duration > 0):
        return False
    merged = {k: v for k, v in session.items() if k != "device"}
    merged.update(position=position, duration=duration, ts=time.time())
    save_entry(cfg, cast(HistoryEntry, merged))
    return True


def recent(cfg: Config, limit: int = 30, typ: str | None = None) -> list[HistoryEntry]:
    entries = [e for e in load_history(cfg).values() if not _watched(e)]
    if typ is not None:
        # Legacy entries without "type" predate series support → treat as "movie"
        # (same default the CLI uses when reading entry types).
        entries = [e for e in entries if e.get("type", "movie") == typ]
    entries.sort(key=lambda e: e.get("ts", 0.0), reverse=True)
    return entries[:limit]
