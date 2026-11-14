"""Desktop observation and session guards, without a receiver or playback ownership."""

import json
import os
import subprocess
import sys

import pytest

from nstream import desktop, headless, state
from nstream.cli_args import build_parser
from nstream.config import Config, PlayOpts

CFG = Config(torrentio_base="unused")
OPTS = PlayOpts(auto=True, cast=False, sub_mode=None, sub_lang=None, history=False, autoplay=False)
SESSION = {"session_id": "first", "device": "192.0.2.1", "title": "Movie"}


def test_observer_opt_in_and_one_inflight_request():
    import threading

    finish = threading.Event()
    calls = []

    def sample(device):
        calls.append(device)
        finish.wait(2)
        return {"telemetry_connected": True, "position": 12}

    observer = desktop.Observer(sample)
    assert observer.tick(SESSION, False, 1)["connection"] == "connecting"
    assert not observer.pending
    observer.tick(SESSION, True, 2)
    observer.tick(SESSION, True, 10)
    finish.set()
    result = observer.results.get(timeout=2)
    observer.results.put(result)
    telemetry = observer.tick(SESSION, False, 11)
    assert calls == [SESSION["device"]]
    assert telemetry["connection"] == "connected"
    assert telemetry["status"]["position"] == 12


def test_disconnection_retains_last_sample_and_timestamp():
    observer = desktop.Observer()
    observer.tick(SESSION, False, 1)
    observer.results.put(("first", {"telemetry_connected": True, "player_state": "PLAYING"}, 100))
    connected = observer.tick(SESSION, False, 2)
    observer.results.put(("first", {}, 101))
    lost = observer.tick(SESSION, False, 3)
    assert lost["connection"] == "disconnected"
    assert lost["sampled_at"] == connected["sampled_at"] == 100
    assert lost["status"]["player_state"] == "PLAYING"


def test_old_session_result_is_discarded_even_on_same_device():
    observer = desktop.Observer()
    observer.tick(SESSION, False, 1)
    observer.results.put(("first", {"telemetry_connected": True, "position": 99}, 100))
    new = {**SESSION, "session_id": "second"}
    result = observer.tick(new, False, 2)
    assert result["status"] is None
    assert result["sampled_at"] is None
    assert result["connection"] == "connecting"
    assert observer.tick(None, False, 3)["connection"] == "idle"


def test_legacy_identity_stable_and_changes_with_generation(monkeypatch):
    raw = {"device": "TV", "title": "Movie", "video_id": "tt1", "ts": 123}
    monkeypatch.setattr(state, "active_cast_info", lambda: None)
    monkeypatch.setattr(state, "cast_session_info", lambda: raw)
    first = desktop.session_info()
    assert first is not None
    assert desktop.session_info() == first
    raw["ts"] = 124
    second = desktop.session_info()
    assert second is not None
    assert second["session_id"] != first["session_id"]


@pytest.mark.parametrize(
    "action", ["--stop", "--pause", "--resume", "--status", "--seek", "--volume", "--sub-shift"]
)
def test_session_guard_rejects_before_any_lifecycle_side_effect(monkeypatch, capsys, action):
    monkeypatch.setattr(desktop, "session_info", lambda: {**SESSION, "session_id": "replacement"})

    def forbidden(*args, **kwargs):
        pytest.fail("must reject before discovery, cleanup or receiver I/O")

    monkeypatch.setattr(headless.remux, "gc_stale", forbidden)
    monkeypatch.setattr(headless.mirror, "stop", forbidden)
    args = ["--json", "--session-id", "first", action]
    if action in ("--seek", "--volume", "--sub-shift"):
        args.append("1")
    assert headless.run(CFG, build_parser().parse_args(args), OPTS) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "session_changed"


@pytest.mark.parametrize("extra", [["--status"], ["--seek", "0"], ["--history"], ["Movie"]])
def test_watch_rejects_other_actions(capsys, extra):
    args = build_parser().parse_args(["--json", "--watch", *extra])
    assert headless.run(CFG, args, OPTS) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage"


def test_watch_real_process_snapshot_heartbeat_eof_and_no_secrets(tmp_path):
    # No active session: the stream must work without catt, a daemon or a provider.
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = str(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "nstream", "--json", "--watch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdout is not None and proc.stdin is not None
    try:
        import select

        assert select.select([proc.stdout], [], [], 10)[0], "watch failed to start"
        first = json.loads(proc.stdout.readline())
        assert first["api_version"] == 1
        assert "session_guard" in first["capabilities"]
        assert first["telemetry"]["connection"] == "idle"
        assert select.select([proc.stdout], [], [], 3)[0], "missing heartbeat"
        second = json.loads(proc.stdout.readline())
        assert second["sequence"] > first["sequence"]
        proc.stdin.write('{"observe":false}\n')
        proc.stdin.flush()
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_history_presence_has_public_session_identity(monkeypatch):
    monkeypatch.setattr(state, "active_cast_info", lambda: {**SESSION, "url": "secret"})
    payload = desktop.history_payload(CFG)
    assert payload["cast_session"] == SESSION
    assert "secret" not in json.dumps(payload)


@pytest.mark.parametrize("payload", ['{"observe":1}\n', "[]\n", "not-json\n", "x" * 4097])
def test_watch_rejects_malformed_or_oversized_interest(payload, tmp_path):
    env = dict(os.environ, XDG_RUNTIME_DIR=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, "-m", "nstream", "--json", "--watch"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,  # Includes cold interpreter/import startup on a busy desktop.
    )
    assert proc.returncode == 2
    snapshot = json.loads(proc.stdout.splitlines()[0])
    assert snapshot["api_version"] == 1


def test_guarded_status_uses_session_address_without_discovery(monkeypatch, capsys):
    monkeypatch.setattr(desktop, "session_info", lambda: SESSION)
    monkeypatch.setattr(
        headless, "_resolve_device", lambda *a, **k: pytest.fail("must not rediscover")
    )
    monkeypatch.setattr(headless.caster, "status", lambda device: {"player_state": "PAUSED"})
    monkeypatch.setattr(headless.remux, "live_alignment", lambda device: None)
    args = build_parser().parse_args(
        ["--json", "--status", "--session-id", "first", "--device", "192.0.2.1"]
    )
    assert headless.run(CFG, args, OPTS) == 0
    assert json.loads(capsys.readouterr().out)["player_state"] == "PAUSED"


def test_guarded_stop_rechecks_session_after_slow_receiver_read(monkeypatch, capsys):
    current = dict(SESSION)
    monkeypatch.setattr(desktop, "session_info", lambda: current)
    monkeypatch.setattr(headless.mirror, "stop", lambda: False)

    def sample(device):
        current["session_id"] = "replacement"
        return {"position": 15, "duration": 100}

    monkeypatch.setattr(headless.caster, "status", sample)
    monkeypatch.setattr(
        headless.caster, "stop", lambda device: pytest.fail("must not stop replacement")
    )
    args = build_parser().parse_args(["--json", "--stop", "--session-id", "first"])
    assert headless.run(CFG, args, OPTS) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "session_changed"


def test_watch_uses_shared_secret_redaction(monkeypatch, capsys):
    monkeypatch.setattr(
        desktop,
        "session_info",
        lambda: {
            **SESSION,
            "title": "Movie https://example.invalid/privatecredential",
        },
    )
    monkeypatch.setattr(desktop.select, "select", lambda *a: ([desktop.sys.stdin], [], []))
    monkeypatch.setattr(desktop.os, "read", lambda *a: b"")

    # pytest's capture stdin intentionally has no file descriptor.
    class Input:
        def fileno(self):
            return 0

    monkeypatch.setattr(desktop.sys, "stdin", Input())
    assert desktop.watch(CFG) == 0
    output = capsys.readouterr().out
    assert "privatecredential" not in output
    assert json.loads(output)["api_version"] == 1


@pytest.mark.parametrize(
    "extra",
    [
        ["--cast"],
        ["--local"],
        ["--follow"],
        ["--quality", "720"],
        ["--audio-lang", "ita"],
        ["--no-history"],
    ],
)
def test_transfer_rejects_ignored_playback_flags(monkeypatch, capsys, extra):
    monkeypatch.setattr(
        headless.handoff, "transfer", lambda *a: pytest.fail("invalid invocation must not transfer")
    )
    args = build_parser().parse_args(
        ["--json", "--transfer-to", "local", "--session-id", "a" * 32, *extra]
    )
    assert headless.run(CFG, args, OPTS) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "usage"


def test_closed_panel_heartbeat_retires_confirmed_detached_cast(monkeypatch, capsys):
    import time

    from nstream.state import active_cast

    active_cast._publish(
        {
            "session_id": "ended",
            "title": "Finished film",
            "device": "TV",
            "ts": time.time(),
            "backend_id": "daemon",
            "receiver_session_id": 1,
        }
    )
    monkeypatch.setattr(
        desktop.bridge,
        "peek_snapshot",
        lambda: {
            "backend_id": "daemon",
            "data": {"session": "idle", "media": {"mediaSessionId": 1, "endReason": "finished"}},
        },
    )
    observer = desktop.Observer(lambda *a: pytest.fail("closed panel must not query TV"))
    monkeypatch.setattr(desktop, "Observer", lambda: observer)
    monkeypatch.setattr(desktop.select, "select", lambda *a: ([desktop.sys.stdin], [], []))
    monkeypatch.setattr(desktop.os, "read", lambda *a: b"")

    class Input:
        def fileno(self):
            return 0

    monkeypatch.setattr(desktop.sys, "stdin", Input())
    assert desktop.watch(Config(history_enabled=False)) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["cast_session"] is None
    assert response["telemetry"]["connection"] == "idle"
