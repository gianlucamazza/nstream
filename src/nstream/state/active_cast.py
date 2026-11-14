"""Playback presence for desktop remotes, independent of watch history (ADR 0054)."""

from __future__ import annotations

import functools
import os
import time
import uuid
from collections.abc import Callable

from .. import util
from .cast_session import CAST_SESSION_TTL

ACTIVE_CAST = "active-cast"


def active_cast_info() -> dict | None:
    """Read confirmed playback presence without discovery or history writes."""
    session = util.RunState(ACTIVE_CAST).read()
    if not session:
        return None
    if session.get("follow"):
        pid = session.get("pid")
        if not isinstance(pid, int) or not util.pid_alive(pid):
            return None
    elif time.time() - (session.get("ts") or 0.0) > CAST_SESSION_TTL:
        return None
    return session


@util.state_update(lambda: util.RunState(ACTIVE_CAST).path)
def _publish(session: dict) -> None:
    util.RunState(ACTIVE_CAST).write(session)


@util.state_update(lambda: util.RunState(ACTIVE_CAST).path)
def _detach(session_id: str) -> None:
    slot = util.RunState(ACTIVE_CAST)
    session = slot.read()
    if session and session.get("session_id") == session_id:
        slot.write({**session, "follow": False, "pid": None, "ts": time.time()})


@util.state_update(lambda: util.RunState(ACTIVE_CAST).path)
def clear_active_cast(*, device: str | None = None, session_id: str | None = None) -> None:
    """An older follower or a stop on another device must not erase a newer cast."""
    slot = util.RunState(ACTIVE_CAST)
    session = slot.read()
    if not session:
        return
    if device and session.get("device") != device:
        return
    if session_id and session.get("session_id") != session_id:
        return
    slot.clear()


def track_cast[R](fn: Callable[..., R]) -> Callable[..., R]:
    """Observe the shared cast flow's backend events, including blocking TUI casts.

    Only a started event publishes presence. A followed call owns the slot until it
    returns; a detached handoff keeps it until stop/TTL. Events still reach the caller.
    """

    @functools.wraps(fn)
    def tracked(*args, **kwargs) -> R:
        follow = kwargs.get("follow", True)
        session = {
            "session_id": uuid.uuid4().hex,
            "title": str(kwargs["title"])[:200],
            "device": str(kwargs["device"])[:80],
            "follow": follow,
            "ts": time.time(),
        }
        if follow:
            session["pid"] = os.getpid()
        callback = kwargs.get("on_event")
        started = False
        detached = False

        def receive(event: dict) -> None:
            nonlocal started, detached
            kind = event.get("kind")
            if kind == "started" and not started:
                started = True
                session["ts"] = time.time()
                _publish(session)
            elif kind == "disconnected" and started:
                # Losing the observer is not evidence that the TV stopped playing.
                detached = True
                _detach(session["session_id"])
            elif kind in ("ended", "failed") and started:
                clear_active_cast(session_id=session["session_id"])
            if callback:
                callback(event)

        kwargs["on_event"] = receive
        try:
            return fn(*args, **kwargs)
        finally:
            if follow and started and not detached:
                clear_active_cast(session_id=session["session_id"])

    return tracked
