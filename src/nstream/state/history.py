"""Watch history and local library (watchlist + recent searches)."""

from __future__ import annotations

import contextlib
import json
import math
import time
from collections.abc import Iterable

from .. import util
from ..config import Config, library_path, state_path
from ..types import HistoryEntry, Meta, Video

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
    data = util.load_json(state_path(), {})
    return {
        key: entry
        for key, entry in data.items()
        if isinstance(entry, dict)
        and all(
            isinstance(entry.get(field, 0), int | float)
            and math.isfinite(entry.get(field, 0))
            and entry.get(field, 0) >= 0
            for field in ("position", "duration", "ts")
        )
    }


# Within this many seconds of the end a title counts as finished, even if the
# fraction is below WATCHED_THRESHOLD (e.g. padded duration / long credits, or
# mpv paused at EOF with keep-open=yes).
END_TAIL_SECONDS = 60.0


def is_watched(entry: HistoryEntry) -> bool:
    """Whether `entry` is effectively finished — no useful resume point left.

    Public because the continuation policy (`series.next_up`, ADR 0029) needs to answer it
    for ONE entry, instead of inferring it from which list the entry arrived in.

    Deliberately more tolerant than the cast's finish heuristic
    (`cast_delivery.CAST_DONE`), and the two must stay in that order: **advance implies
    watched**. They answer different questions — "is there a resume point left?" (this one,
    tolerant: mpv parked at EOF, padded durations) versus "was that `ended` a natural end or a
    manual stop?" (severe: advancing on a deliberate stop is worse than not advancing). If the
    thresholds ever inverted, a cast that counted as finished would stay out of
    `watched_series` and `-c` would replay the episode forever."""
    duration = entry.get("duration") or 0.0
    if duration <= 0:
        return False
    position = entry.get("position") or 0.0
    tail = min(END_TAIL_SECONDS, 0.05 * duration)  # relative, so short clips aren't mislabelled
    return position / duration > WATCHED_THRESHOLD or position >= duration - tail


_watched = is_watched  # module-internal alias (the public name is the one to use)


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


# A "started" entry (duration 0, no real position ever merged) is clutter in
# continue-watching past this age — pruned on every history write.
STARTED_TTL = 7 * 86400.0

LIBRARY_VERSION = 1
MAX_RECENT_SEARCHES = 20
MAX_WATCHLIST = 500


def _library_read() -> dict:
    data = util.load_json(library_path(), {})
    for key in ("watchlist", "searches"):
        if not isinstance(data.get(key, []), list):
            data[key] = []
    return data


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


@util.state_update(library_path, False)
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


@util.state_update(library_path)
def remember_search(cfg: Config, query: str) -> None:
    if not cfg.history_enabled or not query.strip():
        return
    data = _library_read()
    query = query.strip()
    values = [v for v in data.get("searches", []) if isinstance(v, str)]
    values = [v for v in values if v.casefold() != query.casefold()]
    data.update(version=LIBRARY_VERSION, searches=([query] + values)[:MAX_RECENT_SEARCHES])
    _library_write(data)


@util.state_update(state_path)
def save_entry(
    cfg: Config, entry: HistoryEntry, *, drop: Iterable[str] = (), started: bool = False
) -> None:
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
    with contextlib.suppress(OSError):
        history = load_history(cfg)
        if started:
            entry = entry.copy()
            previous = history.get(vid)
            if previous and not entry.get("duration"):
                entry["duration"] = previous.get("duration", 0.0)
            if entry.get("type") == "series" and entry.get("series_id"):
                drop = (
                    *drop,
                    *(
                        key
                        for key, value in history.items()
                        if key != vid
                        and value.get("series_id") == entry["series_id"]
                        and (not value.get("duration") or _watched(value))
                    ),
                )
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
    save_entry(cfg, entry, started=True)


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


def resumable(cfg: Config, limit: int = 30, typ: str | None = None) -> list[HistoryEntry]:
    """Everything the user can continue, most recent first — in-progress titles AND finished
    series episodes (which continue as the *next* episode, `series.next_up`).

    The single answer to "what can I continue?" (ADR 0029). Callers used to merge `recent()`
    and `watched_series()` by hand and rank them differently depending on the branch: with a
    search term the in-progress list won unconditionally, so a half-watched S01E02 from weeks
    ago beat a binge finished minutes ago. One ordering removes that class of bug."""
    entries = [e for e in load_history(cfg).values() if not _watched(e) or _resumes_as_next(e)]
    if typ is not None:
        entries = [e for e in entries if e.get("type", "movie") == typ]
    entries.sort(key=lambda e: e.get("ts", 0.0), reverse=True)
    return entries[:limit]


def _resumes_as_next(entry: HistoryEntry) -> bool:
    """A finished entry that still has a continuation: only a series episode (the next one).
    A finished film is done — it belongs in the library, not in "continue watching"."""
    return entry.get("type") == "series"
