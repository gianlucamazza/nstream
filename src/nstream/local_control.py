"""Bounded IPC for mpv instances nstream owns. Never read or expose the media URL."""

from __future__ import annotations

import json
import math
import socket

from . import languages
from .state import playback_session


class LocalUnavailable(Exception):
    pass


def command(session: dict, args: list, *, timeout: float = 2.0):
    current = playback_session.local_info()
    if not current or current.get("session_id") != session.get("session_id"):
        raise LocalUnavailable("session_changed")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(current["socket"])
            sock.sendall((json.dumps({"command": args, "request_id": 1}) + "\n").encode())
            buffer = b""
            while len(buffer) <= 1 << 20:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    reply = json.loads(line)
                    if isinstance(reply, dict) and reply.get("request_id") == 1:
                        if reply.get("error") != "success":
                            raise LocalUnavailable("local_command_failed")
                        return reply.get("data")
        except (OSError, ValueError, KeyError):
            raise LocalUnavailable("local_unavailable") from None
    raise LocalUnavailable("local_unavailable")


def snapshot(session: dict) -> dict:
    pos = command(session, ["get_property", "time-pos"])
    dur = command(session, ["get_property", "duration"])
    pause = command(session, ["get_property", "pause"])
    buffering = command(session, ["get_property", "paused-for-cache"])
    if not all(
        isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
        for v in (pos, dur)
    ):
        raise LocalUnavailable("local_not_ready")
    return {
        "position": float(pos),
        "duration": float(dur),
        "player_state": "PAUSED" if pause else "BUFFERING" if buffering else "PLAYING",
        "telemetry_connected": True,
    }


def preferences(session: dict) -> dict:
    result = dict(session.get("preferences") or {})
    try:
        tracks = command(session, ["get_property", "track-list"])
        if isinstance(tracks, list):
            for track in tracks:
                if isinstance(track, dict) and track.get("selected") and track.get("lang"):
                    if track.get("type") == "audio":
                        result["audio_lang"] = languages.normalize(
                            str(track["lang"])
                        ) or result.get("audio_lang")
                    elif track.get("type") == "sub":
                        result.update(
                            sub_mode="auto",
                            sub_lang=languages.normalize(str(track["lang"]))
                            or result.get("sub_lang"),
                        )
        visible = command(session, ["get_property", "sub-visibility"])
        sid = command(session, ["get_property", "sid"])
        if not visible or sid is False:
            result.update(sub_mode=None, sub_lang=None)
        delay = command(session, ["get_property", "sub-delay"])
        if isinstance(delay, int | float) and math.isfinite(delay):
            result["sub_offset"] = float(result.get("sub_offset") or 0) + delay
    except LocalUnavailable:
        pass
    return result
