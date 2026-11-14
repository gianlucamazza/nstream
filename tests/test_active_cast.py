"""Desktop presence must exist during blocking playback, without history writes."""

import os
import time

import pytest

from nstream import state, util
from nstream.state import active_cast


def test_followed_cast_publishes_during_playback_and_forwards_events():
    events = []

    @state.track_cast
    def play(**kwargs):
        assert state.active_cast_info() is None
        kwargs["on_event"]({"kind": "started"})
        session = state.active_cast_info()
        assert session is not None
        assert (session["title"], session["device"]) == ("Show", "TV")
        assert session["pid"] == os.getpid()
        assert state.cast_session_info() is None  # no watch-history session needed
        kwargs["on_event"]({"kind": "paused"})
        assert state.active_cast_info() is not None
        return "done"

    assert play(title="Show", device="TV", on_event=events.append) == "done"
    assert [e["kind"] for e in events] == ["started", "paused"]
    assert state.active_cast_info() is None


def test_failed_start_keeps_previous_detached_cast():
    active_cast._publish({"title": "Old", "device": "TV", "ts": time.time()})

    @state.track_cast
    def play(**kwargs):
        kwargs["on_event"]({"kind": "failed"})

    play(title="New", device="TV")
    session = state.active_cast_info()
    assert session is not None and session["title"] == "Old"


def test_followed_exception_cleans_only_its_own_session():
    @state.track_cast
    def play(**kwargs):
        kwargs["on_event"]({"kind": "started"})
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        play(title="Show", device="TV")
    assert state.active_cast_info() is None


def test_detached_session_stop_and_device_guard():
    @state.track_cast
    def play(**kwargs):
        kwargs["on_event"]({"kind": "started"})

    play(title="Show", device="TV", follow=False)
    state.clear_active_cast(device="Other TV")
    state.clear_active_cast(session_id="older-follower")
    assert state.active_cast_info() is not None
    state.clear_active_cast(device="TV")
    assert state.active_cast_info() is None


def test_dead_owner_and_detached_ttl(monkeypatch):
    slot = util.RunState(active_cast.ACTIVE_CAST)
    slot.write({"follow": True, "pid": os.getpid(), "ts": 0})
    assert state.active_cast_info() is not None  # a long film/pause is still live
    monkeypatch.setattr(util, "pid_alive", lambda pid: False)
    assert state.active_cast_info() is None
    slot.write({"follow": False, "ts": 0})
    assert state.active_cast_info() is None


def test_disconnect_keeps_receiver_presence():
    @state.track_cast
    def play(**kwargs):
        kwargs["on_event"]({"kind": "started"})
        kwargs["on_event"]({"kind": "disconnected"})

    play(title="Show", device="TV")
    session = state.active_cast_info()
    assert session is not None and session["follow"] is False


def test_older_follower_cannot_erase_replacement():
    @state.track_cast
    def play(**kwargs):
        kwargs["on_event"]({"kind": "started"})
        active_cast._publish({"session_id": "replacement", "ts": time.time()})

    play(title="Show", device="TV")
    session = state.active_cast_info()
    assert session is not None and session["session_id"] == "replacement"


@pytest.mark.parametrize("reason", ["finished", "stopped", "interrupted"])
def test_terminal_snapshot_retires_matching_detached_presence(reason):
    active_cast._publish(
        {
            "session_id": "same",
            "title": "Film",
            "device": "TV",
            "ts": time.time(),
            "backend_id": "daemon-start",
            "receiver_session_id": 5,
        }
    )
    active_cast.reconcile_terminal(
        {
            "backend_id": "daemon-start",
            "data": {"session": "idle", "media": {"mediaSessionId": 5, "endReason": reason}},
        }
    )
    assert state.active_cast_info() is None


@pytest.mark.parametrize(
    "backend,receiver,reason",
    [
        ("daemon-start", 5, "disconnected"),
        ("daemon-start", 6, "finished"),
        ("new-daemon", 5, "finished"),
        ("daemon-start", 5, None),
    ],
)
def test_uncertain_or_other_generation_snapshot_preserves_presence(backend, receiver, reason):
    active_cast._publish(
        {
            "session_id": "same",
            "title": "Film",
            "device": "TV",
            "ts": time.time(),
            "backend_id": "daemon-start",
            "receiver_session_id": 5,
        }
    )
    active_cast.reconcile_terminal(
        {
            "backend_id": backend,
            "data": {"session": "idle", "media": {"mediaSessionId": receiver, "endReason": reason}},
        }
    )
    current_cast = state.active_cast_info()
    assert current_cast is not None and current_cast["session_id"] == "same"


def test_intentional_reload_rebinds_same_logical_session():
    active_cast._publish(
        {
            "session_id": "same",
            "device": "TV",
            "ts": time.time(),
            "backend_id": "daemon",
            "receiver_session_id": 1,
            "transfer_fingerprint": "old",
        }
    )
    active_cast.rebind_cast("old-session", {"backend_id": "daemon", "receiver_session_id": 2})
    assert (state.active_cast_info() or {})["receiver_session_id"] == 1
    active_cast.rebind_cast("same", {"backend_id": "daemon", "receiver_session_id": 2})
    value = state.active_cast_info() or {}
    assert value["receiver_session_id"] == 2 and "transfer_fingerprint" not in value
    active_cast.reconcile_terminal(
        {"backend_id": "daemon", "data": {"media": {"mediaSessionId": 1, "endReason": "finished"}}}
    )
    assert state.active_cast_info() is not None
    active_cast.reconcile_terminal(
        {"backend_id": "daemon", "data": {"media": {"mediaSessionId": 2, "endReason": "finished"}}}
    )
    assert state.active_cast_info() is None
