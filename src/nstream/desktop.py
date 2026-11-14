"""Versioned desktop snapshots; observation never owns playback (ADR 0057)."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import select
import sys
import threading
import time
from collections.abc import Callable

from . import caster, log, state
from .config import Config

API_VERSION = 1
CAPABILITIES = ["watch", "session_guard", "receiver_interest"]


def session_info() -> dict | None:
    """Public identity, independent of device names, addresses and history preference."""
    raw = state.active_cast_info() or state.cast_session_info()
    if not raw:
        return None
    identity = (
        raw.get("session_id")
        or hashlib.sha256(
            json.dumps([raw.get(k) for k in ("device", "title", "video_id", "ts")]).encode()
        ).hexdigest()
    )
    return {"session_id": identity, "title": raw.get("title", ""), "device": raw.get("device", "")}


def history_payload(cfg: Config, typ: str | None = None) -> dict:
    entries = []
    for entry in state.resumable(cfg, limit=30, typ=typ):
        entries.append(
            {
                **{
                    k: entry[k]
                    for k in ("title", "type", "season", "episode", "position", "duration")
                    if k in entry
                },
                "next": entry.get("type") == "series" and state.is_watched(entry),
            }
        )
    return {"entries": entries, "cast_session": session_info(), "prefer_cast": cfg.prefer_cast}


class Observer:
    """One bounded receiver request at a time; discard results from replaced sessions."""

    def __init__(self, sample: Callable[[str], dict] = caster.status):
        self.sample = sample
        self.session: dict | None = None
        self.status: dict | None = None
        self.sampled_at: float | None = None
        self.connection = "idle"
        self.pending = False
        self.next_sample = 0.0
        self.results: queue.Queue = queue.Queue(maxsize=1)

    def tick(self, session: dict | None, observe: bool, now: float) -> dict:
        if session != self.session:
            self.session = session
            self.status = None
            self.sampled_at = None
            self.connection = "connecting" if session else "idle"
            self.next_sample = 0.0
        try:
            identity, result, sampled_at = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.pending = False
            if session and identity == session["session_id"]:
                if result.get("telemetry_connected"):
                    self.status = result
                    self.sampled_at = sampled_at
                    self.connection = "connected"
                else:
                    self.connection = "disconnected"
        if observe and session and not self.pending and now >= self.next_sample:
            self.pending = True
            self.next_sample = now + 3
            identity, device = session["session_id"], session["device"]

            def read() -> None:
                try:
                    result = self.sample(device)
                except Exception:
                    # Backend failures are telemetry failures, never playback completion.
                    result = {}
                self.results.put((identity, result, time.time()))

            threading.Thread(target=read, daemon=True, name="desktop-status").start()
        return {"connection": self.connection, "sampled_at": self.sampled_at, "status": self.status}


def watch(cfg: Config) -> int:
    """JSONL snapshots, with opt-in receiver interest on stdin; EOF ends observation only."""
    observer = Observer()
    observe = False
    sequence = 0
    last_history = 0.0
    history: dict = {}
    buffer = b""
    persisted_at = None
    try:
        while True:
            now = time.monotonic()
            if now - last_history >= 5 or not history:
                history = history_payload(cfg)
                last_history = now
            session = session_info()
            telemetry = observer.tick(session, observe, now)
            if (
                session
                and telemetry["connection"] == "connected"
                and telemetry["sampled_at"] != persisted_at
            ):
                st = telemetry["status"] or {}
                state.update_from_receiver(
                    cfg,
                    session["device"],
                    st.get("position") or 0,
                    st.get("duration") or 0,
                    title=st.get("title"),
                )
                persisted_at = telemetry["sampled_at"]
            sequence += 1
            log.emit_json(
                {
                    "ok": True,
                    "action": "watch",
                    "api_version": API_VERSION,
                    "capabilities": CAPABILITIES,
                    "sequence": sequence,
                    "captured_at": time.time(),
                    **history,
                    "cast_session": session,
                    "telemetry": telemetry,
                }
            )
            ready, _, _ = select.select([sys.stdin], [], [], 1)
            if ready:
                chunk = os.read(sys.stdin.fileno(), 4096)
                if not chunk:
                    return 0
                buffer += chunk
                if len(buffer) > 4096:
                    return 2
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        request = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        return 2
                    if not isinstance(request, dict) or type(request.get("observe")) is not bool:
                        return 2
                    observe = request["observe"]
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
