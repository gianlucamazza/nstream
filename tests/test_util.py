"""Tests for the shared low-level helpers."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess

import pytest

from nstream import util


def test_atomic_write_creates_file_and_dir(tmp_path):
    path = tmp_path / "sub" / "out.json"
    util.atomic_write(path, lambda f: json.dump({"a": 1}, f), prefix=".out-")
    assert json.loads(path.read_text()) == {"a": 1}
    # 0600 perms, parent dir auto-created.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_write_replaces_existing(tmp_path):
    path = tmp_path / "out.json"
    path.write_text("old")
    util.atomic_write(path, lambda f: f.write("new"), prefix=".out-")
    assert path.read_text() == "new"


def test_atomic_write_no_temp_left_on_success(tmp_path):
    path = tmp_path / "out.json"
    util.atomic_write(path, lambda f: f.write("x"), prefix=".out-")
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_cleans_temp_and_raises_on_error(tmp_path):
    path = tmp_path / "out.json"

    def boom(_f):
        raise OSError("disk full")

    with pytest.raises(OSError):
        util.atomic_write(path, boom, prefix=".out-")
    assert list(tmp_path.glob("*.tmp")) == []  # temp cleaned up
    assert not path.exists()  # target untouched


def test_atomic_write_bytes_roundtrip(tmp_path):
    path = tmp_path / "sub" / "poster.img"
    util.atomic_write_bytes(path, b"\x89PNG\x00", prefix=".poster-")
    assert path.read_bytes() == b"\x89PNG\x00"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.glob("**/*.tmp")) == []


def test_load_json_roundtrip(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"k": "v"}))
    assert util.load_json(path, {}) == {"k": "v"}


def test_load_json_missing_returns_fallback(tmp_path):
    assert util.load_json(tmp_path / "nope.json", {"default": True}) == {"default": True}


def test_load_json_corrupt_returns_fallback(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert util.load_json(path, {}) == {}


def test_load_json_wrong_type_returns_fallback(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")  # a list where a dict was expected
    assert util.load_json(path, {}) == {}


# --- RunState ---------------------------------------------------------------


def test_runstate_path_under_xdg_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert util.RunState("demo").path == tmp_path / "nstream-demo.json"


def test_runstate_path_falls_back_to_tempdir(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(util.tempfile, "gettempdir", lambda: str(tmp_path))
    assert util.RunState("demo").path == tmp_path / "nstream-demo.json"


def test_runstate_roundtrip_and_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    st = util.RunState("demo")
    st.write({"pid": 42, "file": "/x.mp4"})
    assert st.read() == {"pid": 42, "file": "/x.mp4"}
    st.clear()
    assert st.read() is None
    st.clear()  # idempotent: clearing nothing never raises


def test_runstate_read_corrupt_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    st = util.RunState("demo")
    st.path.write_text("{not json")
    assert st.read() is None


def test_runstate_write_is_best_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    st = util.RunState("demo")
    parent = tmp_path / "not-a-directory"
    parent.write_text("occupied")
    st.path = parent / "state.json"  # unwritable: parent is a regular file
    st.write({"a": 1})  # must not raise
    assert st.read() is None


# --- pid_alive / kill_pid ----------------------------------------------------


def test_pid_alive_own_process_and_falsy():
    assert util.pid_alive(os.getpid()) is True
    assert util.pid_alive(None) is False
    assert util.pid_alive(0) is False


def test_pid_alive_dead_process():
    p = subprocess.Popen(["true"])
    p.wait()  # reaped → the pid no longer exists
    assert util.pid_alive(p.pid) is False


def _kill_recorders(monkeypatch):
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(util.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(util.os, "killpg", lambda pid, sig: calls.append(("killpg", pid, sig)))
    return calls


def test_kill_pid_plain_signals_only_the_pid(monkeypatch):
    calls = _kill_recorders(monkeypatch)
    util.kill_pid(123)
    assert calls == [("kill", 123, signal.SIGTERM)]


def test_kill_pid_pgroup_signals_group_then_pid(monkeypatch):
    calls = _kill_recorders(monkeypatch)
    util.kill_pid(123, pgroup=True)
    assert calls == [("killpg", 123, signal.SIGTERM), ("kill", 123, signal.SIGTERM)]


def test_kill_pid_pgroup_survives_failed_killpg(monkeypatch):
    calls: list[tuple[str, int]] = []

    def killpg(pid, sig):
        raise ProcessLookupError  # not a group leader / already gone

    monkeypatch.setattr(util.os, "killpg", killpg)
    monkeypatch.setattr(util.os, "kill", lambda pid, sig: calls.append(("kill", pid)))
    util.kill_pid(123, pgroup=True)  # must not raise
    assert calls == [("kill", 123)]  # the plain kill still runs


def test_kill_pid_none_is_noop(monkeypatch):
    calls = _kill_recorders(monkeypatch)
    util.kill_pid(None)
    util.kill_pid(0, pgroup=True)
    assert calls == []


def test_run_cmd_success():
    proc = util.run_cmd(["printf", "hello"])
    assert proc is not None
    assert proc.stdout == "hello"


def test_run_cmd_missing_binary_returns_none():
    assert util.run_cmd(["nstream-definitely-not-a-binary-xyz"]) is None


def test_run_cmd_timeout_returns_none():
    assert util.run_cmd(["sleep", "5"], timeout=0.1) is None


def test_run_cmd_passes_input():
    proc = util.run_cmd(["cat"], input="piped")
    assert proc is not None
    assert proc.stdout == "piped"


def test_runstate_write_refuses_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    target = tmp_path / "target.json"
    target.write_text('{"a": 1}')
    rs = util.RunState("sym")
    rs.path.symlink_to(target)
    rs.write({"b": 2})  # O_NOFOLLOW → OSError → suppressed, nothing written
    assert target.read_text() == '{"a": 1}'


def test_runstate_write_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    rs = util.RunState("perm")
    rs.write({"a": 1})
    assert rs.read() == {"a": 1}
    assert stat.S_IMODE(rs.path.stat().st_mode) == 0o600
