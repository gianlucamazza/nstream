"""Transfer commits only after observed destination playback; rollback is ownership-safe."""

import json
import os
import types

import pytest

from nstream import handoff, media_identity
from nstream.config import Config
from nstream.state import playback_session


class FakeEndpoint:
    def __init__(
        self, target, identity, events, current, *, position=10, duration=120, paused=False
    ):
        self.target = target
        self.session = {
            "session_id": identity,
            "title": "Movie",
            "video_id": "tt1",
            "type": "movie",
            "release": {"filename": "movie.mkv"},
            "preferences": {},
            "device": "TV",
        }
        self.events = events
        self.current = current
        self.current[target] = self.session
        self.position, self.duration = position, duration
        self.state = "PAUSED" if paused else "PLAYING"
        self.fail = None

    def check(self):
        value = self.current.get(self.target)
        if not value or value["session_id"] != self.session["session_id"]:
            raise handoff.TransferError("session_changed")

    def snapshot(self):
        self.check()
        self.events.append((self.session["session_id"], "sample", self.state))
        return {"position": self.position, "duration": self.duration, "player_state": self.state}

    def control(self, action, value=0):
        self.check()
        self.events.append((self.session["session_id"], action))
        if self.fail == action:
            raise handoff.TransferError("control_failed")
        if action == "pause":
            self.state = "PAUSED"
        elif action == "play":
            self.state = "PLAYING"
        elif action == "seek":
            self.position = value

    def stop(self):
        self.check()
        self.events.append((self.session["session_id"], "stop"))
        if self.fail == "stop":
            raise handoff.TransferError("source_stop_failed")
        self.current[self.target] = None


def pair(monkeypatch, *, paused=False):
    active, events = {}, []
    old = FakeEndpoint("cast", "old", events, active, position=42, paused=paused)
    new = FakeEndpoint("local", "new", events, active, position=1)
    monkeypatch.setattr(handoff, "current", lambda target: active.get(target))
    return old, new, events, active


@pytest.mark.parametrize("paused", [False, True])
def test_transfer_aligns_exact_cursor_and_pause_before_retiring_source(monkeypatch, paused):
    old, new, events, active = pair(monkeypatch, paused=paused)
    result = handoff.commit(old, new, initial=old.snapshot())
    assert result["ok"] and result["source_stopped"]
    assert new.position == 42
    assert new.state == ("PAUSED" if paused else "PLAYING")
    assert events[-1] == ("old", "stop")
    assert (
        events.index(("new", "pause"))
        < events.index(("new", "seek"))
        < events.index(("old", "stop"))
    )
    assert active["local"] is not None and active["cast"] is None


@pytest.mark.parametrize("failure", ["pause", "seek", "play"])
def test_destination_failure_restores_origin_and_stops_only_destination(monkeypatch, failure):
    old, new, events, active = pair(monkeypatch)
    new.fail = failure
    with pytest.raises(handoff.TransferError):
        handoff.commit(old, new, initial=old.snapshot())
    assert active["cast"] is old.session
    assert old.state == "PLAYING"
    assert active["local"] is None
    assert ("old", "stop") not in events


def test_stop_failure_rolls_back_destination_and_restores_origin(monkeypatch):
    old, new, events, active = pair(monkeypatch)
    old.fail = "stop"
    with pytest.raises(handoff.TransferError, match="source_stop_failed"):
        handoff.commit(old, new, initial=old.snapshot())
    assert old.state == "PLAYING" and active["cast"] is old.session
    assert active["local"] is None


def test_different_edition_is_rejected_before_origin_pause(monkeypatch):
    old, new, events, active = pair(monkeypatch)
    new.duration = 150
    with pytest.raises(handoff.TransferError, match="edition_mismatch"):
        handoff.commit(old, new, initial=old.snapshot())
    assert ("old", "pause") not in events and ("old", "stop") not in events
    assert active["cast"] is not None and active["local"] is None


def test_replaced_source_is_not_stopped_or_resumed(monkeypatch):
    old, new, events, active = pair(monkeypatch)
    initial = old.snapshot()
    active["cast"] = {"session_id": "replacement"}
    with pytest.raises(handoff.TransferError, match="session_changed"):
        handoff.commit(old, new, initial=initial)
    assert active["cast"]["session_id"] == "replacement"
    assert not any(event[:2] in (("old", "stop"), ("old", "play")) for event in events)
    assert active["local"] is None


def test_gone_source_after_stop_attempt_keeps_verified_destination(monkeypatch):
    old, new, events, active = pair(monkeypatch)

    def uncertain_stop():
        active["cast"] = None
        raise handoff.TransferError("source_stop_failed")

    old.stop = uncertain_stop
    with pytest.raises(handoff.TransferError, match="transfer_incomplete"):
        handoff.commit(old, new, initial=old.snapshot())
    assert active["local"] is new.session and new.state == "PLAYING"


def test_preparation_failure_never_pauses_origin(monkeypatch):
    old, new, events, active = pair(monkeypatch)
    active["local"] = None
    monkeypatch.setattr(handoff, "Endpoint", lambda target, record: old)

    def spawn(request):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'{"ok":false,"error":"release_unavailable"}\n')
        os.close(write_fd)
        return types.SimpleNamespace(poll=lambda: 0), read_fd

    monkeypatch.setattr(handoff, "_spawn", spawn)
    result = handoff.transfer(Config(history_enabled=False), "old", "local")
    assert not result["ok"] and result["error"] == "release_unavailable"
    assert old.state == "PLAYING" and ("old", "stop") not in events


def test_target_busy_is_rejected_before_starting_worker(monkeypatch):
    old, new, events, active = pair(monkeypatch)
    monkeypatch.setattr(handoff, "_spawn", lambda request: pytest.fail("must not start target"))
    result = handoff.transfer(Config(), "old", "local")
    assert result["error"] == "target_busy"


def test_release_identity_requires_same_torrent_file_or_filename():
    stream = {"infoHash": "a" * 40, "fileIdx": 2, "behaviorHints": {"filename": "folder/movie.mkv"}}
    expected = media_identity.release_key(stream)
    assert expected["filename"] == "movie.mkv"
    assert media_identity.same_release(stream, expected)
    assert not media_identity.same_release({"infoHash": "a" * 40, "fileIdx": 3}, expected)
    assert not media_identity.same_release({"infoHash": "b" * 40}, expected)
    assert not media_identity.release_key({"behaviorHints": {"filename": "https://host/SECRET"}})


def test_local_presence_is_independent_of_history_and_cleanup_is_identity_checked():
    value = playback_session.publish_local(
        {"type": "movie", "video_id": "tt1", "title": "Film", "release": {"filename": "file.mkv"}},
        os.getpid(),
        "/tmp/owned.sock",
        identity="one",
    )
    current_local = playback_session.local_info()
    assert current_local is not None and current_local["session_id"] == "one"
    assert playback_session.transferable(value)
    playback_session.publish_local(value, os.getpid(), "/tmp/new.sock", identity="two")
    playback_session.clear_local("one")
    current_local = playback_session.local_info()
    assert current_local is not None and current_local["session_id"] == "two"
    playback_session.clear_local("two")
    assert playback_session.local_info() is None
    assert "socket" not in json.dumps(playback_session.public_local())


def test_physical_receiver_replacement_is_never_commanded(monkeypatch):
    record = {"session_id": "old", "device": "TV", "title": "Movie"}
    monkeypatch.setattr(handoff, "current", lambda _: record)
    info = {
        "player_state": "PLAYING",
        "position": 42,
        "duration": 120,
        "media_session_id": 1,
        "content_id": "private-media",
        "app_id": "app",
        "session_id": "receiver",
        "media_metadata": {"title": "Movie"},
    }
    monkeypatch.setattr(handoff.caster, "receiver_info", lambda _: info)
    endpoint = handoff.Endpoint("cast", record)
    endpoint.snapshot()
    info["content_id"] = "external-replacement"
    from nstream import cast_control

    monkeypatch.setattr(
        cast_control, "media_control", lambda *a, **k: pytest.fail("must not command replacement")
    )
    monkeypatch.setattr(handoff.caster, "stop", lambda *a: pytest.fail("must not stop replacement"))
    with pytest.raises(handoff.TransferError, match="session_changed"):
        endpoint.control("pause")
    with pytest.raises(handoff.TransferError, match="session_changed"):
        endpoint.stop()


def test_retiring_delivery_never_sends_second_receiver_stop(monkeypatch):
    from nstream import remux

    value = {
        "pid": 123,
        "file": "/private/old.hls",
        "device": "TV",
        "inproc": None,
        "proc_start": "original",
    }
    monkeypatch.setattr(remux, "delivery_identity", lambda _: value)
    events = []
    monkeypatch.setattr(remux, "_teardown", lambda pid, path: events.append((pid, path)))
    remux.retire_delivery(dict(value))
    assert events == [(123, "/private/old.hls")]
    events.clear()
    remux.retire_delivery({**value, "proc_start": "replacement"})
    assert events == []


def test_same_release_filter_prevents_other_edition_selection(monkeypatch, capsys):
    from nstream import headless_play
    from nstream.config import PlayOpts

    monkeypatch.setattr(
        headless_play.api,
        "streams",
        lambda *a: [{"behaviorHints": {"filename": "other-edition.mkv"}}],
    )
    monkeypatch.setattr(
        headless_play.stream_select,
        "prepare_stream",
        lambda *a, **k: pytest.fail("wrong edition reached selection"),
    )
    assert (
        headless_play._select(
            Config(),
            PlayOpts(True, False, None, None, False, False),
            "movie",
            "tt1",
            "Movie",
            release={"filename": "original.mkv"},
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"] == "release_unavailable"


def test_stopping_owned_cast_clears_matching_legacy_presence(monkeypatch):
    from nstream import state, util
    from nstream.state import active_cast

    record = {"session_id": "old", "device": "TV", "title": "Movie", "video_id": "tt1"}
    active_cast._publish({**record, "ts": handoff.time.time()})
    util.RunState("watch").write(
        {"device": "TV", "title": "Movie", "video_id": "tt1", "ts": handoff.time.time()}
    )
    endpoint = handoff.Endpoint("cast", record)
    monkeypatch.setattr(endpoint, "snapshot", lambda: {})
    monkeypatch.setattr(handoff.caster, "stop", lambda *a: True)
    endpoint.stop()
    assert state.active_cast_info() is None and state.cast_session_info() is None


def test_clearing_old_legacy_presence_does_not_erase_replacement():
    from nstream import state, util

    slot = util.RunState("watch")
    old = {"device": "TV", "video_id": "tt1", "ts": handoff.time.time()}
    slot.write(old)
    slot.write({**old, "video_id": "tt2"})
    state.clear_cast_session(expected=old)
    assert (state.cast_session_info() or {})["video_id"] == "tt2"


def test_torrent_pack_hash_alone_does_not_identify_a_media_file():
    assert not media_identity.same_release({"infoHash": "a" * 40}, {"info_hash": "a" * 40})
    assert not media_identity.same_release(
        {"infoHash": "a" * 40, "behaviorHints": {"filename": "episode2.mkv"}},
        {"info_hash": "a" * 40, "filename": "episode1.mkv"},
    )
    assert not playback_session.transferable(
        {"type": "series", "video_id": "tt1:1:1", "release": {"info_hash": "a" * 40}}
    )
