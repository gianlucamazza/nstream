"""Unit tests for the native mirror cast backend (the executor).

Process, Wayland, and PipeWire I/O are mocked: tests never spawn the sender/mpv, talk to
hyprctl/pactl, or touch the receiver. We test the orchestration logic — null-sink and
headless-output lifecycle, window resolution, state tracking, and idempotent teardown.
"""

from __future__ import annotations

import pytest

from nstream import mirror
from nstream.config import Config


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror, "_state_path", lambda: tmp_path / "mirror.json")
    yield


def _cfg(**kw) -> Config:
    return Config(torrentio_base="tb", **kw)


# --- available() ----------------------------------------------------------


def test_available_false_when_binary_missing(monkeypatch):
    monkeypatch.setattr(mirror, "_sender_bin", lambda: "/no/such/cast_sender")
    assert mirror.available() is False


def test_available_false_when_helpers_missing(monkeypatch, tmp_path):
    fake = tmp_path / "cast_sender"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(mirror, "_sender_bin", lambda: str(fake))
    monkeypatch.setattr(mirror.shutil, "which", lambda _: None)
    assert mirror.available() is False


def test_available_true_when_all_present(monkeypatch, tmp_path):
    fake = tmp_path / "cast_sender"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(mirror, "_sender_bin", lambda: str(fake))
    monkeypatch.setattr(mirror.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert mirror.available() is True


# --- null sink ------------------------------------------------------------


def test_load_null_sink_parses_module_id(monkeypatch):
    class _P:
        returncode = 0
        stdout = "536870916\n"

    monkeypatch.setattr(mirror.subprocess, "run", lambda *a, **k: _P())
    assert mirror._load_null_sink() == 536870916


def test_load_null_sink_none_on_failure(monkeypatch):
    class _P:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(mirror.subprocess, "run", lambda *a, **k: _P())
    assert mirror._load_null_sink() is None


# --- hyprctl resolution ---------------------------------------------------


def test_window_addr_matches_pid(monkeypatch):
    clients = [
        {"pid": 111, "address": "0xAAA"},
        {"pid": 222, "address": "0xBBB"},
    ]
    monkeypatch.setattr(mirror, "_hypr_json", lambda *a: clients)
    assert mirror._window_addr(222) == "0xBBB"
    assert mirror._window_addr(999) is None


def test_headless_workspace(monkeypatch):
    mons = [
        {"name": "eDP-1", "activeWorkspace": {"id": 3}},
        {"name": "HEADLESS-2", "activeWorkspace": {"id": 7}},
    ]
    monkeypatch.setattr(mirror, "_hypr_json", lambda *a: mons)
    assert mirror._headless_workspace("HEADLESS-2") == 7
    assert mirror._headless_workspace("DP-9") is None


def test_create_headless_returns_new_name(monkeypatch):
    names = [{"eDP-1", "DP-2"}, {"eDP-1", "DP-2", "HEADLESS-3"}]
    calls = {"n": 0}

    def _hl():
        i = min(calls["n"], len(names) - 1)
        calls["n"] += 1
        return names[i]

    monkeypatch.setattr(mirror, "_headless_names", _hl)
    monkeypatch.setattr(mirror, "_hypr", lambda *a: "")
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    assert mirror._create_headless() == "HEADLESS-3"


# --- state + teardown -----------------------------------------------------


def test_state_roundtrip_and_clear():
    mirror._write_state({"mpv_pid": 1, "sender_pid": 2})
    assert mirror._read_state() == {"mpv_pid": 1, "sender_pid": 2}
    mirror._clear_state()
    assert mirror._read_state() is None


def test_teardown_is_complete_and_idempotent(monkeypatch, tmp_path):
    killed: list[int] = []
    unloaded: list[int] = []
    removed: list[str] = []
    monkeypatch.setattr(mirror, "_kill", lambda pid: killed.append(pid) if pid else None)
    monkeypatch.setattr(mirror, "_unload_module", lambda mid: unloaded.append(mid) if mid else None)
    monkeypatch.setattr(
        mirror, "_hypr", lambda *a: removed.append(a[-1]) if a[:2] == ("output", "remove") else ""
    )
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)

    work_dir = tmp_path / "nstream-mirror-x"
    work_dir.mkdir()
    (work_dir / "mpv.sock").write_bytes(b"")
    state = {
        "sender_pid": 2, "mpv_pid": 1, "headless": "HEADLESS-2",
        "sink_module": 99, "work_dir": str(work_dir),
    }  # fmt: skip
    mirror._teardown(state)
    assert killed == [2, 1]
    assert unloaded == [99]
    assert removed == ["HEADLESS-2"]
    assert not work_dir.exists()  # per-cast work dir reclaimed, no leak until logout
    assert mirror._read_state() is None
    # Second teardown: nothing left to clear, no crash.
    assert mirror.stop() is False


def test_stop_tears_down_persisted_state(monkeypatch, tmp_path):
    """stop() (detached path) also removes the per-cast work dir recorded in the state."""
    monkeypatch.setattr(mirror, "_kill", lambda pid: None)
    monkeypatch.setattr(mirror, "_unload_module", lambda mid: None)
    monkeypatch.setattr(mirror, "_hypr", lambda *a: "")
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    work_dir = tmp_path / "nstream-mirror-y"
    work_dir.mkdir()
    mirror._write_state(
        {"sender_pid": 5, "headless": "HEADLESS-1", "sink_module": 7, "work_dir": str(work_dir)}
    )
    assert mirror.stop() is True
    assert not work_dir.exists()
    assert mirror._read_state() is None


# --- cast_via_mirror guards ----------------------------------------------


def test_cast_via_mirror_degrades_when_unavailable(monkeypatch):
    monkeypatch.setattr(mirror, "available", lambda: False)
    assert mirror.cast_via_mirror(_cfg(), "Film", "http://u", device="1.2.3.4") == (0.0, 0.0, False)


# --- cast_via_mirror orchestration -----------------------------------------


class _MpvProc:
    def __init__(self, pid=4242, wait_exc=None):
        self.pid = pid
        self.wait_calls = 0
        self._wait_exc = wait_exc

    def wait(self):
        self.wait_calls += 1
        if self._wait_exc:
            raise self._wait_exc


def _wire(monkeypatch, tmp_path, *, proc=None):
    """Stub every external step of cast_via_mirror (sink, headless output, mpv, window,
    sender) with recorders; each test then breaks exactly ONE step and asserts the
    already-mounted ones are unwound (`_teardown` is recorded with a state snapshot —
    its real dismantling is covered by test_teardown_is_complete_and_idempotent)."""
    rec: dict = {"teardowns": [], "killed": [], "popen": [], "hypr": []}
    proc = proc or _MpvProc()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))  # work dirs under tmp
    monkeypatch.setattr(mirror, "available", lambda: True)
    monkeypatch.setattr(mirror, "_load_null_sink", lambda: 99)
    monkeypatch.setattr(mirror, "_create_headless", lambda: "HEADLESS-9")
    monkeypatch.setattr(mirror, "_headless_workspace", lambda name: 7)
    monkeypatch.setattr(mirror, "_await_window", lambda pid: "0xAAA")
    monkeypatch.setattr(mirror, "_window_addr", lambda pid: "0xAAA")
    monkeypatch.setattr(mirror, "_launch_sender", lambda cfg, dev, addr: 555)
    monkeypatch.setattr(mirror, "_hypr", lambda *a: rec["hypr"].append(a) or "")
    monkeypatch.setattr(mirror, "_kill", lambda pid: rec["killed"].append(pid))
    monkeypatch.setattr(mirror, "_teardown", lambda st: rec["teardowns"].append(dict(st)))
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)

    def popen(args, **kw):
        rec["popen"].append((list(args), kw))
        return proc

    monkeypatch.setattr(mirror.subprocess, "Popen", popen)
    return rec, proc


def test_cast_via_mirror_clears_stale_state_first(monkeypatch, tmp_path):
    """A leftover mirror from a previous run is torn down before mounting the new one
    (single active mirror)."""
    rec, _ = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(mirror, "_create_headless", lambda: None)  # end the run early
    mirror._write_state({"mpv_pid": 1, "headless": "HEADLESS-1"})
    mirror.cast_via_mirror(_cfg(), "F", "http://u", device="1.2.3.4", follow=False)
    assert rec["teardowns"][0] == {"mpv_pid": 1, "headless": "HEADLESS-1"}


def test_cast_via_mirror_headless_failure_unwinds_sink(monkeypatch, tmp_path):
    """Step 2 (headless output) fails → step 1 (null sink) is unwound, mpv never spawns."""
    rec, _ = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(mirror, "_create_headless", lambda: None)
    out = mirror.cast_via_mirror(_cfg(), "F", "http://u", device="1.2.3.4", follow=False)
    assert out == (0.0, 0.0, False)
    st = rec["teardowns"][-1]
    assert st["sink_module"] == 99
    assert "headless" not in st and "mpv_pid" not in st and "sender_pid" not in st
    assert rec["popen"] == []  # mpv never launched


def test_cast_via_mirror_mpv_missing_unwinds_sink_and_headless(monkeypatch, tmp_path):
    """Step 3 (mpv) fails → sink + headless output are unwound."""
    rec, _ = _wire(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise FileNotFoundError("mpv")

    monkeypatch.setattr(mirror.subprocess, "Popen", boom)
    out = mirror.cast_via_mirror(_cfg(), "F", "http://u", device="1.2.3.4", follow=False)
    assert out == (0.0, 0.0, False)
    st = rec["teardowns"][-1]
    assert st["sink_module"] == 99 and st["headless"] == "HEADLESS-9"
    assert "mpv_pid" not in st and "sender_pid" not in st


def test_cast_via_mirror_mpv_oserror_unwinds_sink_and_headless(monkeypatch, tmp_path):
    """Any OSError from the mpv Popen (not just FileNotFoundError — e.g. a
    PermissionError on the binary) degrades cleanly: sink + headless unwound,
    no orphans, no propagation."""
    rec, _ = _wire(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise PermissionError("mpv")

    monkeypatch.setattr(mirror.subprocess, "Popen", boom)
    out = mirror.cast_via_mirror(_cfg(), "F", "http://u", device="1.2.3.4", follow=False)
    assert out == (0.0, 0.0, False)
    st = rec["teardowns"][-1]
    assert st["sink_module"] == 99 and st["headless"] == "HEADLESS-9"
    assert "mpv_pid" not in st and "sender_pid" not in st


def test_cast_via_mirror_window_timeout_kills_mpv_and_unwinds(monkeypatch, tmp_path):
    """Step 4 (window appears) fails → mpv is killed and everything mounted unwound."""
    rec, _ = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(mirror, "_await_window", lambda pid: None)
    out = mirror.cast_via_mirror(_cfg(), "F", "http://u", device="1.2.3.4", follow=False)
    assert out == (0.0, 0.0, False)
    assert 4242 in rec["killed"]
    st = rec["teardowns"][-1]
    assert st["mpv_pid"] == 4242 and st["headless"] == "HEADLESS-9" and st["sink_module"] == 99
    assert "sender_pid" not in st


def test_cast_via_mirror_sender_failure_unwinds_everything(monkeypatch, tmp_path):
    """Step 5 (sender) fails → mpv killed, sink/headless/mpv unwound, no state persisted."""
    rec, _ = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(mirror, "_launch_sender", lambda cfg, dev, addr: None)
    out = mirror.cast_via_mirror(_cfg(), "F", "http://u", device="1.2.3.4", follow=False)
    assert out == (0.0, 0.0, False)
    assert 4242 in rec["killed"]
    st = rec["teardowns"][-1]
    assert st["mpv_pid"] == 4242 and st["headless"] == "HEADLESS-9" and st["sink_module"] == 99
    assert "sender_pid" not in st
    assert mirror._read_state() is None  # nothing for --stop: the failure cleaned up


def test_cast_via_mirror_headless_success_detaches_and_keeps_state(monkeypatch, tmp_path):
    """follow=False happy path: mpv detached (outlives nstream), full state persisted
    for --stop, mpv moved to the headless workspace, nothing torn down."""
    rec, proc = _wire(monkeypatch, tmp_path)
    out = mirror.cast_via_mirror(_cfg(), "F", "http://u", device="1.2.3.4", follow=False)
    assert out == (0.0, 0.0, False)
    assert rec["teardowns"] == []  # the cast keeps running
    _args, kw = rec["popen"][0]
    assert kw["start_new_session"] is True  # detached session
    assert proc.wait_calls == 0
    st = mirror._read_state()
    work_dir = st.pop("work_dir")  # dynamic mkdtemp path, checked separately below
    assert st == {
        "device": "1.2.3.4", "sink_module": 99, "headless": "HEADLESS-9",
        "mpv_pid": 4242, "sender_pid": 555,
    }  # fmt: skip
    assert ("dispatch", "movetoworkspacesilent", "7,address:0xAAA") in rec["hypr"]
    # The per-cast work dir (mpv IPC socket) stays alive for the detached mpv, but is
    # recorded in the state so stop() can reclaim it (it used to leak until logout).
    dirs = list(tmp_path.glob("nstream-mirror-*"))
    assert [str(d) for d in dirs] == [work_dir]


def test_cast_via_mirror_follow_tracks_position_and_tears_down(monkeypatch, tmp_path):
    """follow=True happy path: position tracked over IPC, blocking wait on mpv, then
    full teardown and work-dir cleanup."""
    rec, proc = _wire(monkeypatch, tmp_path)

    def fake_track(sock, holder, p):
        holder["position"] = 12.0
        holder["duration"] = 99.0

    monkeypatch.setattr(mirror.player, "_track_position", fake_track)
    out = mirror.cast_via_mirror(_cfg(), "F", "http://u", device="1.2.3.4", follow=True)
    assert out == (12.0, 99.0, False)
    assert proc.wait_calls == 1
    _args, kw = rec["popen"][0]
    assert kw["start_new_session"] is False  # follow: mpv dies with us
    assert rec["teardowns"][-1]["sender_pid"] == 555  # full unwind at playback end
    assert list(tmp_path.glob("nstream-mirror-*")) == []  # work dir removed


def test_cast_via_mirror_follow_ctrl_c_kills_mpv_and_tears_down(monkeypatch, tmp_path):
    """Ctrl-C while following: mpv killed, teardown still runs (finally), and the call
    returns normally — the KeyboardInterrupt is swallowed (photographed)."""
    rec, _proc = _wire(monkeypatch, tmp_path, proc=_MpvProc(wait_exc=KeyboardInterrupt()))
    monkeypatch.setattr(mirror.player, "_track_position", lambda *a: None)
    out = mirror.cast_via_mirror(_cfg(), "F", "http://u", device="1.2.3.4", follow=True)
    assert out == (0.0, 0.0, False)
    assert 4242 in rec["killed"]
    assert rec["teardowns"] != []


# --- _mpv_args / _launch_sender --------------------------------------------


def _stub_player_defaults(monkeypatch):
    monkeypatch.setattr(mirror.player, "_quiet_defaults", lambda cfg: ["--quiet-x"])
    monkeypatch.setattr(mirror.player, "_hwdec_defaults", lambda cfg: ["--hwdec=vaapi"])
    monkeypatch.setattr(mirror.player, "_lang_defaults", lambda cfg: [])


def test_mpv_args_route_audio_to_null_sink(monkeypatch):
    _stub_player_defaults(monkeypatch)
    args = mirror._mpv_args(
        _cfg(), "http://u", "/run/mpv.sock",
        start=None, sub_paths=(), audio_id=None, sub_id=None,
    )  # fmt: skip
    assert args[0] == "mpv" and args[-1] == "http://u"
    assert "--audio-device=pipewire/nstream_cast" in args
    assert f"--title={mirror._MPV_TITLE}" in args
    assert "--input-ipc-server=/run/mpv.sock" in args
    assert "--quiet-x" in args and "--hwdec=vaapi" in args  # player defaults inherited
    assert not any(a.startswith(("--start", "--aid", "--sid", "--sub-file")) for a in args)


def test_mpv_args_start_tracks_and_subs(monkeypatch):
    _stub_player_defaults(monkeypatch)
    args = mirror._mpv_args(
        _cfg(), "http://u", "/s",
        start=42.0, sub_paths=("/a.srt",), audio_id=2, sub_id="3",
    )  # fmt: skip
    assert "--start=42" in args
    assert "--sub-file=/a.srt" in args and "--aid=2" in args and "--sid=3" in args
    # Same gate as player.play: a sub-second resume is noise, not a position.
    args2 = mirror._mpv_args(
        _cfg(), "http://u", "/s", start=0.5, sub_paths=(), audio_id=None, sub_id=None
    )
    assert not any(a.startswith("--start") for a in args2)


def test_launch_sender_argv_defaults_and_detach(monkeypatch):
    rec: dict = {}

    class _P:
        pid = 555

    def popen(cmd, **kw):
        rec["cmd"], rec["kw"] = list(cmd), kw
        return _P()

    monkeypatch.setattr(mirror.subprocess, "Popen", popen)
    monkeypatch.setattr(mirror, "_sender_bin", lambda: "/bin/cast_sender")
    assert mirror._launch_sender(_cfg(), "10.0.0.5", "0xAAA") == 555
    cmd = rec["cmd"]
    assert cmd[0] == "/bin/cast_sender"
    assert cmd[cmd.index("-m") + 1] == "16000000"  # built-in bitrate default
    assert cmd[cmd.index("--playout-delay") + 1] == "500"  # roomy movie buffer
    assert cmd[cmd.index("--audio-sink") + 1] == "nstream_cast"
    assert "10.0.0.5:8009" in cmd and "window:addr=0xAAA" in cmd
    assert rec["kw"]["start_new_session"] is True


def test_launch_sender_config_overrides_and_failure(monkeypatch):
    rec: dict = {}

    class _P:
        pid = 555

    def popen(cmd, **kw):
        rec["cmd"] = list(cmd)
        return _P()

    monkeypatch.setattr(mirror.subprocess, "Popen", popen)
    monkeypatch.setattr(mirror, "_sender_bin", lambda: "/bin/cast_sender")
    cfg = _cfg(mirror_bitrate=8_000_000, mirror_playout_ms=200)
    mirror._launch_sender(cfg, "10.0.0.5", "0xBBB")
    assert rec["cmd"][rec["cmd"].index("-m") + 1] == "8000000"
    assert rec["cmd"][rec["cmd"].index("--playout-delay") + 1] == "200"

    def boom(*a, **k):
        raise OSError("exec")

    monkeypatch.setattr(mirror.subprocess, "Popen", boom)
    assert mirror._launch_sender(cfg, "10.0.0.5", "0xBBB") is None
