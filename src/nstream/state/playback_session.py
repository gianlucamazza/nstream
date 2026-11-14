"""Owned playback identity and local IPC presence, independent of watch history."""

from __future__ import annotations

import contextlib
import contextvars
import uuid
from collections.abc import Callable

from .. import util
from ..config import PlayOpts

# Only playback preferences cross process boundaries. No source URL or credentials.
PREFERENCES = (
    "audio_lang",
    "sub_mode",
    "sub_lang",
    "quality",
    "sub_offset",
    "sub_scale",
    "history",
)
operation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "transfer_operation", default=None
)
local_context: contextvars.ContextVar[tuple[dict, Callable | None]] = contextvars.ContextVar(
    "local_playback", default=({}, None)
)


def preferences(opts: PlayOpts) -> dict:
    return {name: getattr(opts, name) for name in PREFERENCES}


def media(typ: str, video_id: str, title: str, opts: PlayOpts) -> dict:
    value: dict = {
        "type": typ,
        "video_id": video_id,
        "title": title[:200],
        "preferences": preferences(opts),
    }
    if operation_id.get():
        value["handoff_id"] = operation_id.get()
    return value


@contextlib.contextmanager
def local_request(metadata: dict, ready: Callable | None = None):
    token = local_context.set((metadata, ready))
    try:
        yield
    finally:
        local_context.reset(token)


def local_info() -> dict | None:
    value = util.RunState("active-local").read()
    if not value or not value.get("pid") or not util.pid_alive(value["pid"]):
        return None
    return value


@util.state_update(lambda: util.RunState("active-local").path, {})
def publish_local(
    metadata: dict, pid: int, socket_path: str, *, identity: str | None = None
) -> dict:
    value = {
        **metadata,
        "session_id": identity or uuid.uuid4().hex,
        "pid": pid,
        "socket": socket_path,
        "target": "local",
    }
    slot = util.RunState("active-local")
    slot.write(value)
    return value if (slot.read() or {}).get("session_id") == value["session_id"] else {}


@util.state_update(lambda: util.RunState("active-local").path)
def clear_local(identity: str) -> None:
    slot = util.RunState("active-local")
    value = slot.read()
    if value and value.get("session_id") == identity:
        slot.clear()


def public_local() -> dict | None:
    value = local_info()
    if value is None:
        return None
    return {key: value.get(key) for key in ("session_id", "title", "type", "video_id")}


def transferable(value: dict | None) -> bool:
    return bool(
        value
        and value.get("video_id")
        and (
            (value.get("release") or {}).get("filename")
            or ((value.get("release") or {}).get("info_hash") and "file_index" in value["release"])
        )
        and value.get("type") in ("movie", "series")
        and value.get("delivery") != "mirror"
    )


@util.state_update(lambda: util.RunState("active-cast").path)
def stamp_cast(identity: str, fingerprint: str) -> None:
    slot = util.RunState("active-cast")
    value = slot.read()
    if value and value.get("session_id") == identity:
        slot.write({**value, "transfer_fingerprint": fingerprint})
