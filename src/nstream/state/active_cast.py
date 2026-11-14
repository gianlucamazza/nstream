"""Playback presence for desktop remotes, independent of watch history (ADR 0054)."""

from __future__ import annotations

import functools
import os
import time
import uuid
from collections.abc import Callable

from .. import media_identity, util
from .cast_session import CAST_SESSION_TTL
from .playback_session import media as playback_media

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
        session: dict = {
            "session_id": uuid.uuid4().hex,
            "title": str(kwargs["title"])[:200],
            "device": str(kwargs["device"])[:80],
            "follow": follow,
            "ts": time.time(),
        }
        if kwargs.get("video_id") and kwargs.get("opts"):
            session.update(
                playback_media(
                    kwargs.get("typ", "movie"), kwargs["video_id"], session["title"], kwargs["opts"]
                )
            )
        chosen = kwargs.get("chosen") or (args[2] if len(args) > 2 else {})
        if isinstance(chosen, dict):
            session["release"] = media_identity.release_key(chosen)
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
                if event.get("backend_id") and event.get("receiver_session_id") is not None:
                    session["backend_id"] = event["backend_id"]
                    session["receiver_session_id"] = event["receiver_session_id"]
                if event.get("release"):
                    session["release"] = event["release"]
                if event.get("delivery"):
                    session["delivery"] = event["delivery"]
                if event.get("engine_port"):
                    session["engine_port"] = event["engine_port"]
                if event.get("audio_lang"):
                    session.setdefault("preferences", {})["audio_lang"] = event["audio_lang"]
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


@util.state_update(lambda: util.RunState(ACTIVE_CAST).path)
def rebind_cast(identity: str, event: dict) -> None:
    """An intentional HLS reload keeps the logical session and changes receiver identity."""
    slot = util.RunState(ACTIVE_CAST)
    value = slot.read()
    if (
        value
        and value.get("session_id") == identity
        and event.get("backend_id")
        and event.get("receiver_session_id") is not None
    ):
        value.update(
            backend_id=event["backend_id"], receiver_session_id=event["receiver_session_id"]
        )
        value.pop("transfer_fingerprint", None)
        slot.write(value)


@util.state_update(lambda: util.RunState(ACTIVE_CAST).path)
def reconcile_terminal(snapshot: dict) -> None:
    """Retire only a terminal event bound to this daemon generation and receiver session."""
    slot = util.RunState(ACTIVE_CAST)
    active = slot.read()
    data = snapshot.get("data")
    media = data.get("media") if isinstance(data, dict) else None
    if (
        not active
        or not isinstance(media, dict)
        or not active.get("backend_id")
        or snapshot.get("backend_id") != active["backend_id"]
        or media.get("mediaSessionId") != active.get("receiver_session_id")
        or media.get("endReason") not in ("finished", "stopped", "interrupted")
    ):
        return
    slot.clear()
    # Do not let the legacy fallback resurrect this same retired playback.
    from .cast_session import cast_session_info, clear_cast_session

    legacy = cast_session_info()
    if legacy and legacy.get("device") == active.get("device"):
        same = (
            legacy.get("video_id") == active.get("video_id")
            if active.get("video_id")
            else legacy.get("title") == active.get("title")
        )
        if same:
            clear_cast_session(expected=legacy)
