"""castbridge IPC client (`bridge.py`): the `media-load` arg builder, event decoding, and a
`cast_load` round-trip driven over a `socketpair` standing in for the daemon — no real daemon,
no network."""

from __future__ import annotations

import json
import socket
import threading
import time

from nstream import bridge


def test_media_load_args_omits_empty():
    args = bridge._media_load_args("1.2.3.4", "http://x/y")
    assert args == {"ip": "1.2.3.4", "url": "http://x/y"}  # no empty optional fields


def test_media_load_args_includes_metadata():
    args = bridge._media_load_args(
        "ip",
        "url",
        title="Dune",
        poster="http://p.jpg",
        subtitle="2021",
        series_title="",
        season=0,
        episode=0,
        content_type="video/mp4",
        current_time=42.0,
    )
    assert args["title"] == "Dune"
    assert args["poster"] == "http://p.jpg"
    assert args["subtitle"] == "2021"
    assert args["contentType"] == "video/mp4"
    assert args["currentTime"] == 42.0
    assert "seriesTitle" not in args and "season" not in args  # zero/empty dropped


def test_media_load_args_series():
    args = bridge._media_load_args("ip", "url", series_title="Show", season=2, episode=5)
    assert args["seriesTitle"] == "Show"
    assert args["season"] == 2
    assert args["episode"] == 5


def test_media_load_args_current_time_gate():
    # A near-zero resume point isn't worth sending (matches the >1 gate elsewhere).
    assert "currentTime" not in bridge._media_load_args("ip", "url", current_time=0.5)


def test_media_block_from_media_status():
    block = bridge._media_block({"type": "media-status", "data": {"state": "PLAYING"}})
    assert block == {"state": "PLAYING"}
    assert bridge._media_block({"type": "media-status", "data": None}) is None


def test_media_block_from_session():
    msg = {"type": "session", "data": {"session": "media", "media": {"state": "PAUSED"}}}
    assert bridge._media_block(msg) == {"state": "PAUSED"}
    # A non-media session (mirror/youtube/idle) means our media session is gone.
    assert bridge._media_block({"type": "session", "data": {"session": "mirror"}}) is None


def test_progress_extracts_fields():
    state, pos, dur, title = bridge._progress(
        {"state": "PLAYING", "position": 12.5, "duration": 100.0, "title": "X"}
    )
    assert (state, pos, dur, title) == ("PLAYING", 12.5, 100.0, "X")
    assert bridge._progress({}) == ("", 0.0, 0.0, "")


def _fake_daemon(monkeypatch, frames):
    """Wire bridge to a socketpair; write `frames` (dicts) as the daemon's replies/events on the
    server side, return the server socket so the test can close it. cast_load reads them as if
    from the real daemon."""
    client, server = socket.socketpair()
    monkeypatch.setattr(bridge, "ensure_daemon", lambda *a, **k: True)
    monkeypatch.setattr(bridge, "_connect", lambda *a, **k: client)
    for fr in frames:
        server.sendall((json.dumps(fr) + "\n").encode())
    return server


def test_cast_load_follow_emits_started_playing_ended(monkeypatch):
    frames = [
        {"id": 1, "action": "media-load", "ok": True, "data": {"loaded": True}},
        {
            "type": "media-status",
            "data": {"state": "PLAYING", "title": "Dune", "position": 1.0, "duration": 100.0},
        },
        {"type": "media-status", "data": {"state": "PLAYING", "position": 50.0, "duration": 100.0}},
        {"type": "media-status", "data": None},  # ended
    ]
    server = _fake_daemon(monkeypatch, frames)
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=True, title="Dune"))
    finally:
        server.close()
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "started"
    assert "playing" in kinds
    assert kinds[-1] == "ended"
    assert events[-1]["position"] == 50.0


def test_cast_load_no_follow_returns_after_started(monkeypatch):
    frames = [
        {"id": 1, "action": "media-load", "ok": True, "data": {"loaded": True}},
        {"type": "media-status", "data": {"state": "BUFFERING", "title": "Dune"}},
    ]
    server = _fake_daemon(monkeypatch, frames)
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=False, title="Dune"))
    finally:
        server.close()
    assert [e["kind"] for e in events] == ["started"]


def test_cast_load_failed_load(monkeypatch):
    frames = [
        {
            "id": 1,
            "action": "media-load",
            "ok": False,
            "error": {"code": "no_devices", "message": "device not found"},
        },
    ]
    server = _fake_daemon(monkeypatch, frames)
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=True))
    finally:
        server.close()
    assert events == [{"kind": "failed", "error": "no_devices", "message": "device not found"}]


def test_cast_load_survives_read_timeout(monkeypatch):
    """A quiet stretch (recv timeout) must NOT be mistaken for end-of-stream: the generator
    has to keep following and report the *real* later position, not end at the first tick."""
    monkeypatch.setattr(bridge, "_FOLLOW_TIMEOUT", 0.1)
    monkeypatch.setattr(bridge, "_LOAD_TIMEOUT", 1.0)
    client, server = socket.socketpair()
    monkeypatch.setattr(bridge, "ensure_daemon", lambda *a, **k: True)
    monkeypatch.setattr(bridge, "_connect", lambda *a, **k: client)

    def send(obj):
        server.sendall((json.dumps(obj) + "\n").encode())

    send({"id": 1, "action": "media-load", "ok": True, "data": {"loaded": True}})
    send({"type": "media-status", "data": {"state": "PLAYING", "position": 5.0, "duration": 100.0}})

    def later():
        time.sleep(0.35)  # > _FOLLOW_TIMEOUT → forces ≥1 timeout tick first
        send(
            {
                "type": "media-status",
                "data": {"state": "PLAYING", "position": 50.0, "duration": 100.0},
            }
        )
        time.sleep(0.25)
        send({"type": "media-status", "data": None})  # real end

    t = threading.Thread(target=later)
    t.start()
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=True, title="X"))
    finally:
        t.join()
        server.close()
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "started" and kinds[-1] == "ended"
    # The post-timeout position must have been observed (old bug ended at the first tick).
    assert any(e["kind"] == "playing" and e["position"] == 50.0 for e in events)
    assert events[-1]["position"] == 50.0
