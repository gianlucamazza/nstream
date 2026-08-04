"""Persistent client state: history, library, cast session, dead sources.

Public API is re-exported here so `from nstream import state` keeps a stable surface.
"""

from __future__ import annotations

from .cast_session import (
    CAST_SESSION,
    CAST_SESSION_TTL,
    cast_session_device,
    clear_cast_session,
    expire_cast_session,
    remember_cast,
    update_from_receiver,
)
from .dead import (
    DEAD_TTL,
    MAX_DEAD_SOURCES,
    dead_sources,
    forget_dead,
    is_dead,
    mark_dead,
)
from .history import (
    END_TAIL_SECONDS,
    LIBRARY_VERSION,
    MAX_RECENT_SEARCHES,
    MAX_WATCHLIST,
    STARTED_TTL,
    WATCHED_THRESHOLD,
    is_watched,
    is_watchlisted,
    load_history,
    make_entry,
    note_started,
    recent,
    recent_searches,
    remember_search,
    resumable,
    resume_position,
    save_entry,
    toggle_watchlist,
    watched_series,
    watchlist,
)

__all__ = [
    "MAX_WATCHLIST",
    "MAX_RECENT_SEARCHES",
    "LIBRARY_VERSION",
    "STARTED_TTL",
    "END_TAIL_SECONDS",
    "CAST_SESSION",
    "CAST_SESSION_TTL",
    "DEAD_TTL",
    "MAX_DEAD_SOURCES",
    "WATCHED_THRESHOLD",
    "clear_cast_session",
    "dead_sources",
    "expire_cast_session",
    "forget_dead",
    "is_dead",
    "is_watchlisted",
    "load_history",
    "make_entry",
    "mark_dead",
    "note_started",
    "recent",
    "recent_searches",
    "remember_cast",
    "remember_search",
    "resume_position",
    "save_entry",
    "toggle_watchlist",
    "update_from_receiver",
    "cast_session_device",
    "is_watched",
    "resumable",
    "watched_series",
    "watchlist",
]
