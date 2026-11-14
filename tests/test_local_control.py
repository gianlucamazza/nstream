"""Owned mpv IPC, including a real local-only playback lifecycle."""

import json
import os
import shutil
import socket
import threading
import wave

import pytest

from nstream import local_control, player
from nstream.config import Config
from nstream.state import playback_session


def test_ipc_rejects_replaced_owner_before_connect(monkeypatch):
    monkeypatch.setattr(playback_session, "local_info", lambda: {"session_id": "new"})
    with pytest.raises(local_control.LocalUnavailable, match="session_changed"):
        local_control.command({"session_id": "old"}, ["quit"])


def test_ipc_ignores_events_and_matches_reply(tmp_path):
    path = str(tmp_path / "mpv.sock")
    session = playback_session.publish_local({}, os.getpid(), path)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen()
        seen = []

        def receive():
            conn, _ = server.accept()
            with conn:
                seen.append(json.loads(conn.recv(4096)))
                conn.sendall(b'{"event":"property-change"}\n{"request_id":9,"data":99}\n')
                conn.sendall(b'{"request_id":1,"error":"success","data":42}\n')

        thread = threading.Thread(target=receive)
        thread.start()
        assert local_control.command(session, ["get_property", "time-pos"]) == 42
        thread.join(2)
    assert seen == [{"command": ["get_property", "time-pos"], "request_id": 1}]


@pytest.mark.parametrize("position", [float("nan"), float("inf"), -1, True, None])
def test_snapshot_requires_finite_numeric_position(monkeypatch, position):
    values = iter([position, 120, False, False])
    monkeypatch.setattr(local_control, "command", lambda *a: next(values))
    with pytest.raises(local_control.LocalUnavailable):
        local_control.snapshot({})


@pytest.mark.skipif(shutil.which("mpv") is None, reason="local mpv smoke requires mpv")
def test_real_mpv_presence_pause_seek_quit_without_history(tmp_path):
    media = tmp_path / "fixture.wav"
    with wave.open(str(media), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", ""))
        output.writeframes(b"\x00\x00" * 8000 * 30)
    ready = threading.Event()
    result, errors = [], []
    metadata = {
        "title": "Fixture",
        "type": "movie",
        "video_id": "tt1",
        "preferences": {"history": False},
        "release": {"filename": "fixture.wav"},
    }

    def run():
        try:
            with playback_session.local_request(metadata, lambda _: ready.set()):
                result.append(
                    player.play(
                        Config(mpv_args=["--no-config", "--ao=null", "--vo=null", "--pause"]),
                        "Fixture",
                        str(media),
                        work_dir=str(tmp_path),
                    )
                )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    session = None
    try:
        assert ready.wait(10), errors
        session = playback_session.local_info()
        assert session is not None
        snapshot = local_control.snapshot(session)
        assert snapshot["player_state"] == "PAUSED"
        assert snapshot["duration"] == pytest.approx(30, abs=0.1)
        local_control.command(session, ["seek", 10, "absolute+exact"])
        assert local_control.snapshot(session)["position"] == pytest.approx(10, abs=0.5)
        local_control.command(session, ["set_property", "pause", False])
        assert local_control.snapshot(session)["player_state"] == "PLAYING"
    finally:
        if session and thread.is_alive():
            local_control.command(session, ["quit"])
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert result and result[0].started
    assert playback_session.local_info() is None


@pytest.mark.skipif(shutil.which("mpv") is None, reason="local worker smoke requires mpv")
def test_transfer_worker_survives_ack_and_owns_player_until_terminated(tmp_path):
    import select
    import subprocess
    import sys

    media = tmp_path / "fixture.wav"
    with wave.open(str(media), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", ""))
        output.writeframes(b"\x00\x00" * 8000 * 30)
    bootstrap = """
import sys
from nstream import application, handoff, subs
from nstream.config import Config
def local(cfg, args, opts, typ, video_id, title, imdb_id, season, episode, selection, **kw):
    assert (typ, video_id, season, episode) == ("series", "tt1:2:3", 2, 3)
    assert kw["release"] == {"filename": "fixture.wav"} and kw["start_override"] == 10
    request = application.LocalRequest(
        title=title, typ=typ, video_id=video_id,
        stream={"url": sys.argv[2], "behaviorHints": {"filename": "fixture.wav"}},
        opts=opts, work_dir=sys.argv[3], start=kw["start_override"],
        on_started=kw["on_started"],
    )
    application.play_local(
        Config(mpv_args=["--no-config", "--ao=null", "--vo=null", "--pause"]),
        request, acquire_subs=lambda *a, **k: subs.SubsPick([]),
    )
    return 0
handoff.headless_play.auto_play = local
raise SystemExit(handoff.worker(int(sys.argv[1])))
"""
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap, str(write_fd), str(media), str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        pass_fds=(write_fd,),
        start_new_session=True,
    )
    os.close(write_fd)
    assert proc.stdin is not None
    request = {
        "target": "local",
        "type": "series",
        "video_id": "tt1:2:3",
        "operation": "a" * 32,
        "title": "Fixture",
        "preferences": {"history": False},
        "release": {"filename": "fixture.wav"},
        "start": 10,
    }
    proc.stdin.write((json.dumps(request) + "\n").encode())
    proc.stdin.close()
    session = None
    try:
        assert select.select([read_fd], [], [], 10)[0]
        reply = json.loads(os.read(read_fd, 8192))
        assert reply["ok"], reply
        session = playback_session.local_info()
        assert session and session["session_id"] == reply["session_id"]
        assert session["handoff_id"] == "a" * 32
        assert proc.poll() is None  # ACK is not the worker's lifetime.
        assert local_control.snapshot(session)["position"] == pytest.approx(10, abs=0.5)
        proc.terminate()
        proc.wait(timeout=10)
        assert playback_session.local_info() is None
        assert not player.util.pid_alive(session["pid"])
    finally:
        os.close(read_fd)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if session and player.util.pid_alive(session["pid"]):
            player.util.kill_pid(session["pid"])
