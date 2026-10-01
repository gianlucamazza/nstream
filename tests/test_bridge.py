"""castbridge IPC client (`bridge.py`): the `media-load` arg builder, event decoding, and a
`cast_load` round-trip driven over a `socketpair` standing in for the daemon — no real daemon,
no network."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from nstream import bridge

# The conftest autouse guard stubs `bridge.ensure_daemon` (and `bridge_available`) on every
# test so nothing ever spawns a real daemon. To test ensure_daemon ITSELF we capture the real
# function at import time (collection runs before fixtures patch the module attribute).
_REAL_ENSURE_DAEMON = bridge.ensure_daemon


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


def test_media_load_args_app_id():
    """appId is forwarded only when set (dormant ADR 0013 hook); absent → no key (default
    receiver, back-compat)."""
    assert "appId" not in bridge._media_load_args("ip", "url")
    assert bridge._media_load_args("ip", "url", app_id="ABCD1234")["appId"] == "ABCD1234"


def test_media_load_args_series():
    args = bridge._media_load_args("ip", "url", series_title="Show", season=2, episode=5)
    assert args["seriesTitle"] == "Show"
    assert args["season"] == 2
    assert args["episode"] == 5


def test_media_load_args_subtitle_track():
    args = bridge._media_load_args(
        "ip", "url", subtitle_url="http://h/subs.vtt", subtitle_lang="eng", subtitle_name="English"
    )
    assert args["subtitleUrl"] == "http://h/subs.vtt"
    assert args["subtitleLang"] == "eng"
    assert args["subtitleName"] == "English"


def test_media_load_args_subtitle_lang_dropped_without_url():
    # A language with no track URL is meaningless → the whole track block is omitted.
    args = bridge._media_load_args("ip", "url", subtitle_lang="eng")
    assert "subtitleUrl" not in args and "subtitleLang" not in args


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


def test_ensure_runtime_dir_tmp_fallback_creates_private_dir(monkeypatch, tmp_path):
    """No XDG_RUNTIME_DIR → the fallback dir is created 0o700 and accepted."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    d = tmp_path / "castbridge-fake"
    monkeypatch.setattr(bridge, "_runtime_dir", lambda: str(d))
    assert bridge._ensure_runtime_dir() is True
    st = os.lstat(d)
    assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700


def test_ensure_daemon_degrades_on_untrusted_tmp_dir(monkeypatch, tmp_path):
    """A pre-created symlink or loose-mode fallback dir (multi-user /tmp attack) must make
    ensure_daemon return False — clean degradation to catt, never a crash.

    Calls the REAL function (the conftest guard stubs `bridge.ensure_daemon`)."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(bridge, "_connect", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "bridge_available", lambda: True)
    # Symlink in place of the dir (attacker redirects the socket/lock elsewhere).
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "castbridge-link"
    link.symlink_to(target)
    monkeypatch.setattr(bridge, "_runtime_dir", lambda: str(link))
    assert _REAL_ENSURE_DAEMON(timeout=0.1) is False
    # Real dir but world-accessible (attacker pre-created it before us).
    loose = tmp_path / "castbridge-loose"
    loose.mkdir(mode=0o777)
    monkeypatch.setattr(bridge, "_runtime_dir", lambda: str(loose))
    assert _REAL_ENSURE_DAEMON(timeout=0.1) is False


# --- ensure_daemon: connect / spawn / flock / poll -------------------------


class _FakeSock:
    """Stands in for the AF_UNIX socket _connect returns: ensure_daemon only close()s it."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _forbid_spawn(monkeypatch):
    """Any subprocess use in this test is a failure (no daemon must be spawned)."""
    monkeypatch.setattr(
        bridge.subprocess, "run", lambda *a, **k: pytest.fail("subprocess.run called")
    )
    monkeypatch.setattr(
        bridge.subprocess, "Popen", lambda *a, **k: pytest.fail("subprocess.Popen called")
    )


def test_ensure_daemon_no_binary_returns_false_without_spawn(monkeypatch):
    """Socket dead + castbridge binary absent → False immediately, nothing spawned."""
    monkeypatch.setattr(bridge, "_connect", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "bridge_available", lambda: False)
    _forbid_spawn(monkeypatch)
    assert _REAL_ENSURE_DAEMON(timeout=0.1) is False


def test_ensure_daemon_already_running_returns_true_without_spawn(monkeypatch):
    """A connectable socket on the first try → True, the probe socket closed, no spawn,
    no lock taken (bridge_available isn't even consulted)."""
    sock = _FakeSock()
    monkeypatch.setattr(bridge, "_connect", lambda *a, **k: sock)
    monkeypatch.setattr(bridge, "bridge_available", lambda: pytest.fail("checked too early"))
    _forbid_spawn(monkeypatch)
    assert _REAL_ENSURE_DAEMON(timeout=0.1) is True
    assert sock.closed


def test_ensure_daemon_flock_recheck_skips_spawn(monkeypatch, tmp_path):
    """Concurrent start: the pre-lock connect fails, but the re-check UNDER the flock finds
    the socket (the other holder spawned it meanwhile) → True without a second spawn."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "bridge_available", lambda: True)
    _forbid_spawn(monkeypatch)
    socks: list[_FakeSock] = []

    def connect(*a, **k):
        if not socks:  # first (pre-lock) probe: nothing listening yet
            socks.append(_FakeSock())  # sentinel: the first probe did not connect
            return None
        socks.append(_FakeSock())
        return socks[-1]

    monkeypatch.setattr(bridge, "_connect", connect)
    assert _REAL_ENSURE_DAEMON(timeout=0.1) is True
    assert socks[-1].closed
    assert os.path.exists(os.path.join(tmp_path, "castbridge", "spawn.lock"))


@pytest.mark.parametrize("systemd_failure", ["raises", "nonzero_rc"])
def test_ensure_daemon_popen_fallback_when_systemd_run_fails(
    monkeypatch, tmp_path, systemd_failure
):
    """systemd-run absent (OSError) or failing (rc != 0) → detached Popen fallback spawns the
    daemon; once the socket appears the poll returns True."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(bridge, "_binary", lambda: "/opt/castbridge")
    monkeypatch.setattr(bridge, "_DAEMON_POLL", 0.01)
    state = {"running": False}
    monkeypatch.setattr(
        bridge, "_connect", lambda *a, **k: _FakeSock() if state["running"] else None
    )

    def fake_run(cmd, **kw):
        assert cmd[0] == "systemd-run"
        if systemd_failure == "raises":
            raise FileNotFoundError("systemd-run")
        return SimpleNamespace(returncode=1)

    popen_calls = []

    def fake_popen(cmd, **kw):
        popen_calls.append((cmd, kw))
        state["running"] = True
        return SimpleNamespace()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(bridge.subprocess, "Popen", fake_popen)
    assert _REAL_ENSURE_DAEMON(timeout=1.0) is True
    (cmd, kw) = popen_calls[0]
    assert cmd == ["/opt/castbridge", "--daemon"]
    assert kw.get("start_new_session") is True  # must outlive its client


def test_ensure_daemon_spawns_transient_unit_via_systemd_run(monkeypatch, tmp_path):
    """The happy spawn path: systemd-run --user --collect launches the daemon; the socket
    poll then connects → True. The Popen fallback must NOT run."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(bridge, "_binary", lambda: "/opt/castbridge")
    monkeypatch.setattr(bridge, "_DAEMON_POLL", 0.01)
    state = {"running": False}
    monkeypatch.setattr(
        bridge, "_connect", lambda *a, **k: _FakeSock() if state["running"] else None
    )
    run_cmds = []

    def fake_run(cmd, **kw):
        run_cmds.append(cmd)
        state["running"] = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(
        bridge.subprocess, "Popen", lambda *a, **k: pytest.fail("Popen fallback used")
    )
    assert _REAL_ENSURE_DAEMON(timeout=1.0) is True
    cmd = run_cmds[0]
    assert cmd[0] == "systemd-run" and "--user" in cmd and "--collect" in cmd
    assert cmd[-2:] == ["/opt/castbridge", "--daemon"]


def test_ensure_daemon_both_spawns_fail_returns_false(monkeypatch, tmp_path):
    """systemd-run AND Popen both fail → clean False (caller degrades to catt), no crash,
    no socket poll wait."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(bridge, "_connect", lambda *a, **k: None)

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(bridge.subprocess, "run", boom)
    monkeypatch.setattr(bridge.subprocess, "Popen", boom)
    t0 = time.monotonic()
    assert _REAL_ENSURE_DAEMON(timeout=5.0) is False
    assert time.monotonic() - t0 < 1.0  # returned without entering the poll loop


def test_ensure_daemon_socket_never_appears_returns_false_and_releases_lock(monkeypatch, tmp_path):
    """Spawn 'succeeds' but the socket never shows up within the deadline → False; the
    spawn.lock fd is closed in the finally (the flock is re-acquirable afterwards)."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(bridge, "_binary", lambda: "/opt/castbridge")
    monkeypatch.setattr(bridge, "_DAEMON_POLL", 0.01)
    monkeypatch.setattr(bridge, "_connect", lambda *a, **k: None)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(
        bridge.subprocess, "Popen", lambda *a, **k: pytest.fail("Popen fallback used")
    )
    assert _REAL_ENSURE_DAEMON(timeout=0.05) is False
    # Cleanup: the lock must have been released (LOCK_NB would raise if still held).
    fd = os.open(bridge._lock_path(), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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


def test_cast_load_eof_mid_cast_yields_disconnected(monkeypatch):
    """A daemon crash (socket EOF after `started`, no explicit end) must NOT be normalized
    into a fake `ended`: the receiver may still be streaming — emit `disconnected` so the
    caller doesn't tear the served file down mid-playback and --follow doesn't lie."""
    frames = [
        {"id": 1, "action": "media-load", "ok": True, "data": {"loaded": True}},
        {
            "type": "media-status",
            "data": {"state": "PLAYING", "title": "X", "position": 42.0, "duration": 100.0},
        },
    ]
    server = _fake_daemon(monkeypatch, frames)
    server.shutdown(socket.SHUT_WR)  # daemon dies: EOF without a media-session end
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=True, title="X"))
    finally:
        server.close()
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "started"
    assert kinds[-1] == "disconnected"
    assert "ended" not in kinds
    assert events[-1]["position"] == 42.0 and events[-1]["duration"] == 100.0


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


def test_cast_load_pre_start_deadline_yields_startup_failed(monkeypatch):
    """Daemon up and load acknowledged, but the receiver never reaches a playing state:
    the timeout ticks must trip the pre-start deadline and yield `cast_startup_failed`
    (the event that triggers caster's fallback to catt) instead of hanging forever."""
    monkeypatch.setattr(bridge, "_LOAD_TIMEOUT", 0.1)
    monkeypatch.setattr(bridge, "_FOLLOW_TIMEOUT", 0.03)
    client, server = socket.socketpair()
    monkeypatch.setattr(bridge, "ensure_daemon", lambda *a, **k: True)
    monkeypatch.setattr(bridge, "_connect", lambda *a, **k: client)
    # Ack the load, then go silent (socket stays open: ticks, not EOF).
    server.sendall(
        (json.dumps({"id": 1, "action": "media-load", "ok": True, "data": {}}) + "\n").encode()
    )
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=True, title="X"))
    finally:
        server.close()
    assert [e["kind"] for e in events] == ["failed"]
    assert events[0]["error"] == "cast_startup_failed"


def test_cast_load_eof_before_any_state_no_follow_yields_started(monkeypatch):
    """Photographs current behavior: load acked, then daemon EOF before any media-status,
    with follow=False → best-effort `started` (the LOAD was acknowledged, the session lives
    in the daemon; a fire-and-return caller must not see a failure)."""
    frames = [{"id": 1, "action": "media-load", "ok": True, "data": {"loaded": True}}]
    server = _fake_daemon(monkeypatch, frames)
    server.shutdown(socket.SHUT_WR)  # EOF right after the ack, before any state
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=False, title="Dune"))
    finally:
        server.close()
    assert events == [{"kind": "started", "title": "Dune"}]


def test_cast_load_eof_without_load_ack_is_not_started(monkeypatch):
    """No media-load ack and no state before EOF: nothing proves a handoff (ADR 0031), so
    a fire-and-return caller gets a failure, never an invented `started`."""
    server = _fake_daemon(monkeypatch, [])
    server.shutdown(socket.SHUT_WR)
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=False, title="Dune"))
    finally:
        server.close()
    assert [e["kind"] for e in events] == ["failed"]
    assert events[0]["error"] == "cast_startup_failed"


def test_cast_load_paused_then_resumed_transitions(monkeypatch):
    """State transitions after `started`: PLAYING→PAUSED emits `paused`, PAUSED→PLAYING
    emits `playing` again, then the inactive media session ends the follow."""
    frames = [
        {"id": 1, "action": "media-load", "ok": True, "data": {"loaded": True}},
        {"type": "media-status", "data": {"state": "PLAYING", "position": 10.0, "duration": 100.0}},
        {"type": "media-status", "data": {"state": "PAUSED", "position": 12.0, "duration": 100.0}},
        {"type": "media-status", "data": {"state": "PLAYING", "position": 12.0, "duration": 100.0}},
        {"type": "media-status", "data": None},  # ended
    ]
    server = _fake_daemon(monkeypatch, frames)
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=True, title="X"))
    finally:
        server.close()
    kinds = [e["kind"] for e in events]
    assert kinds == ["started", "paused", "playing", "ended"]
    paused = events[1]
    assert paused["position"] == 12.0
    assert events[2]["position"] == 12.0 and events[2]["duration"] == 100.0


# --- receiver track/error observability (ADR 0016) --------------------------


def test_active_tracks_helper():
    assert bridge._active_tracks({"activeTrackIds": [1, 2]}) == [1, 2]
    assert bridge._active_tracks({"activeTrackIds": []}) == []
    assert bridge._active_tracks({}) == []  # absent → unknown, never a downgrade
    assert bridge._active_tracks({"activeTrackIds": "x"}) == []  # malformed
    assert bridge._active_tracks({"activeTrackIds": [1, "x", 2]}) == [1, 2]  # ints only


def test_cast_load_events_carry_active_tracks(monkeypatch):
    """The receiver's confirmed activeTrackIds ride the started/playing events (ADR 0016) —
    a caption track (id 1) confirmed active is the receiver's own acknowledgement."""
    frames = [
        {"id": 1, "action": "media-load", "ok": True, "data": {"loaded": True}},
        {
            "type": "media-status",
            "data": {
                "state": "PLAYING",
                "title": "Dune",
                "position": 1.0,
                "duration": 100.0,
                "activeTrackIds": [1],
            },
        },
        {"type": "media-status", "data": None},
    ]
    server = _fake_daemon(monkeypatch, frames)
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=True, title="Dune"))
    finally:
        server.close()
    started = events[0]
    assert started["kind"] == "started" and started["tracks"] == [1]


def test_cast_load_receiver_error_before_start_fails(monkeypatch):
    """A receiver error status (LOAD_FAILED/idleReason ERROR) before `started` → a `failed`
    event so the caller falls back, instead of hanging until the load timeout."""
    frames = [
        {"id": 1, "action": "media-load", "ok": True, "data": {"loaded": True}},
        {"type": "media-status", "data": {"error": "LOAD_FAILED", "state": ""}},
    ]
    server = _fake_daemon(monkeypatch, frames)
    try:
        events = list(bridge.cast_load("ip", "http://x", follow=True))
    finally:
        server.close()
    assert events == [{"kind": "failed", "error": "receiver_error", "message": "LOAD_FAILED"}]


def test_media_load_args_text_language():
    args = bridge._media_load_args("ip", "url", text_language="it")
    assert args["textLanguage"] == "it"
    assert "textLanguage" not in bridge._media_load_args("ip", "url")
