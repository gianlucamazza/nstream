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
import re
import time
from collections.abc import Iterable, Iterator
from typing import cast

from . import util
from .config import Config, HistoryEntry, Meta, Video, library_path, state_path

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


# A "started" entry (duration 0, no real position ever merged) is clutter in
# continue-watching past this age — pruned on every history write.
STARTED_TTL = 7 * 86400.0

LIBRARY_VERSION = 1
MAX_RECENT_SEARCHES = 20
MAX_WATCHLIST = 500


def _library_read() -> dict:
    data = util.load_json(library_path(), {})
    return data if isinstance(data, dict) else {}


def _library_write(data: dict) -> None:
    library_path().parent.mkdir(parents=True, exist_ok=True)
    util.atomic_write(
        library_path(),
        lambda f: json.dump(data, f, ensure_ascii=False),
        prefix=".library-",
    )


def watchlist(cfg: Config) -> list[Meta]:
    """Return locally saved titles, newest first, tolerating old/corrupt entries."""
    if not cfg.history_enabled:
        return []
    entries = _library_read().get("watchlist", [])
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("id") and e.get("name")]


def is_watchlisted(cfg: Config, video_id: str) -> bool:
    return any(m.get("id") == video_id for m in watchlist(cfg))


def toggle_watchlist(cfg: Config, meta: Meta) -> bool:
    """Toggle a title and return its new state. Watchlist entries are metadata-only."""
    if not cfg.history_enabled or not meta.get("id"):
        return False
    data = _library_read()
    entries = [e for e in data.get("watchlist", []) if isinstance(e, dict)]
    video_id = meta["id"]
    if any(e.get("id") == video_id for e in entries):
        entries = [e for e in entries if e.get("id") != video_id]
        enabled = False
    else:
        fields = ("id", "type", "name", "releaseInfo", "poster", "imdbRating", "genres")
        compact = {k: meta[k] for k in fields if k in meta}
        entries.insert(0, compact)
        entries = entries[:MAX_WATCHLIST]
        enabled = True
    data.update(version=LIBRARY_VERSION, watchlist=entries)
    _library_write(data)
    return enabled


def recent_searches(cfg: Config) -> list[str]:
    if not cfg.history_enabled:
        return []
    values = _library_read().get("searches", [])
    return [v for v in values if isinstance(v, str) and v.strip()][:MAX_RECENT_SEARCHES]


def remember_search(cfg: Config, query: str) -> None:
    if not cfg.history_enabled or not query.strip():
        return
    data = _library_read()
    query = query.strip()
    values = [v for v in data.get("searches", []) if isinstance(v, str)]
    values = [v for v in values if v.casefold() != query.casefold()]
    data.update(version=LIBRARY_VERSION, searches=([query] + values)[:MAX_RECENT_SEARCHES])
    _library_write(data)


def save_entry(cfg: Config, entry: HistoryEntry, *, drop: Iterable[str] = ()) -> None:
    """Persist `entry` (or retire it when watched). `drop` retires additional video_ids
    in the same locked write — used by `note_started` to replace a binge's stale
    zero-progress siblings without a second read-modify-write cycle."""
    if not cfg.history_enabled:
        return
    vid = entry.get("video_id")
    if not vid:
        return
    with contextlib.suppress(OSError):  # atomic_write recreates it (and surfaces the error)
        state_path().parent.mkdir(parents=True, exist_ok=True)
    with _history_lock():
        history = load_history(cfg)
        for stale in drop:
            history.pop(stale, None)
        now = time.time()
        for aged in [
            k
            for k, e in history.items()
            # Aged clutter: zero-progress "started" entries, and watched series episodes
            # kept only for next-episode resume (a resume that never came in a week
            # isn't coming — the binge is over or moved on).
            if (not e.get("duration") or (e.get("type") == "series" and _watched(e)))
            and now - (e.get("ts") or 0.0) > STARTED_TTL
        ]:
            history.pop(aged, None)
        if not _watched(entry):
            history[vid] = entry
        elif entry.get("type") == "series":
            # Keep the finished episode (recent() hides it via _watched) so a headless
            # resume can compute "next episode"; it is retired when the next one starts
            # (note_started drops watched siblings).
            history[vid] = entry
        else:
            history.pop(vid, None)

        util.atomic_write(
            state_path(),
            lambda f: json.dump(history, f, ensure_ascii=False),
            prefix=".history-",
        )


def note_started(cfg: Config, entry: HistoryEntry) -> None:
    """Record that playback of `entry` started when the end position can't be known
    (headless fire-and-return cast: no poll loop runs). Keeps a previously known
    duration so the watched/near-end logic stays meaningful; the real position lands
    later via `update_from_receiver` (`--stop`/`--status`). For a series, sibling
    episodes still at zero progress are retired in the same write — a binge leaves
    only the latest started episode, while siblings with real progress stay."""
    history = load_history(cfg)
    prev = history.get(entry.get("video_id", ""))
    if prev and not entry.get("duration"):
        entry["duration"] = prev.get("duration", 0.0)
    drop: tuple[str, ...] = ()
    if entry.get("type") == "series" and entry.get("series_id"):
        # Retire zero-progress siblings (a binge leaves only the latest started) AND
        # watched ones (kept only so resume could advance — this start IS the advance).
        drop = tuple(
            vid
            for vid, e in history.items()
            if e.get("series_id") == entry["series_id"]
            and vid != entry.get("video_id")
            and (not e.get("duration") or _watched(e))
        )
    save_entry(cfg, entry, drop=drop)


# RunState slot for the receiver-side session of a fire-and-return cast: which entry is
# on the TV, so a later `--stop`/`--status` can attribute the receiver's position to it.
CAST_SESSION = "watch"

# Past this age a session no longer plausibly describes what's on the TV (any film plus
# a generous pause fits well within it; every new cast rewrites the session anyway).
CAST_SESSION_TTL = 6 * 3600.0


def clear_cast_session() -> None:
    """Drop the fire-and-return cast session, if any. Called at the start of every new
    cast (`cast_flow.run_cast` / Alt-C): the new content replaces what the session
    described, and a stale session would attribute the receiver's position to it."""
    util.RunState(CAST_SESSION).clear()


def expire_cast_session() -> None:
    """Best-effort: drop the cast session once its TTL has passed. Called at every
    headless entry, so an agent that never issues `--stop` doesn't leave a dead session
    around for a later poll to trip on."""
    run_state = util.RunState(CAST_SESSION)
    session = run_state.read()
    if session and time.time() - (session.get("ts") or 0.0) > CAST_SESSION_TTL:
        run_state.clear()


def _norm_title(s: str) -> str:
    """Casefold + alnum-only for tolerant title comparison. Deliberate small duplicate
    of `headless._norm_title`: state must not import headless (layering)."""
    return "".join(c for c in s.casefold() if c.isalnum())


def _session_title_matches(session_title: str, receiver_title: str) -> bool:
    """Whether the receiver's now-playing title plausibly IS the session's content.
    The receiver title varies by sender — castbridge reports the decorated display
    title ("Mr. Robot · S01E04 · …"), catt the release filename, and the Tier-2 catt
    fallback our own `cast-*.mp4` temp name — so match by normalized substring in
    either direction, and treat an empty/artifact title as not-applicable (True:
    the session TTL decides alone)."""
    r = _norm_title(receiver_title)
    if not r or re.fullmatch(r"cast[0-9a-z_]*mp4", r):
        return True
    s = _norm_title(session_title)
    if not s:
        return True
    return s in r or r in s


def remember_cast(cfg: Config, entry: HistoryEntry, device: str | None) -> None:
    """Persist the fire-and-return cast session (entry + device) across nstream runs.
    Interactive and `--follow` casts don't need this — their poll loop saves directly."""
    if not cfg.history_enabled:
        return
    util.RunState(CAST_SESSION).write({**entry, "device": device or ""})


def update_from_receiver(
    cfg: Config,
    device: str | None,
    position: float,
    duration: float,
    *,
    title: str | None = None,
    clear: bool = False,
) -> bool:
    """Merge a receiver-reported position into the session entry saved by `remember_cast`,
    if one exists for `device` and still plausibly describes what the TV is playing.
    Returns True when the merged position was accepted (written, or the entry retired by
    the watched logic). `clear` drops the session file afterwards (the `--stop` one-shot);
    a zero/idle position still clears but persists nothing. Two staleness guards protect
    the entry from a position that belongs to some other content: the session TTL, and an
    opportunistic match of `title` (the receiver's now-playing title, when it carries one)
    against the session's — a stale session is dropped so later polls can't corrupt it."""
    run_state = util.RunState(CAST_SESSION)
    session = run_state.read()
    if not session:
        return False
    if time.time() - (session.get("ts") or 0.0) > CAST_SESSION_TTL:
        run_state.clear()
        return False
    # No clear on a device mismatch: the session may belong to another (still live) TV.
    if device and session.get("device") and session["device"] != device:
        return False
    if title and not _session_title_matches(session.get("title") or "", title):
        run_state.clear()  # the TV is playing something else: this session is dead
        return False
    if clear:
        run_state.clear()
    if not (position > 0 and duration > 0):
        return False
    merged = {k: v for k, v in session.items() if k != "device"}
    merged.update(position=position, duration=duration, ts=time.time())
    save_entry(cfg, cast(HistoryEntry, merged))
    return True


def watched_series(cfg: Config) -> list[HistoryEntry]:
    """Finished series episodes, most recent first. Kept in history (hidden from
    `recent()` by the watched logic) exactly for this: a headless resume on a finished
    episode advances to the next one instead of coming up empty."""
    entries = [e for e in load_history(cfg).values() if e.get("type") == "series" and _watched(e)]
    entries.sort(key=lambda e: e.get("ts", 0.0), reverse=True)
    return entries


def recent(cfg: Config, limit: int = 30, typ: str | None = None) -> list[HistoryEntry]:
    entries = [e for e in load_history(cfg).values() if not _watched(e)]
    if typ is not None:
        # Legacy entries without "type" predate series support → treat as "movie"
        # (same default the CLI uses when reading entry types).
        entries = [e for e in entries if e.get("type", "movie") == typ]
    entries.sort(key=lambda e: e.get("ts", 0.0), reverse=True)
    return entries[:limit]
