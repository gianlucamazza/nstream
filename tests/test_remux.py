"""Unit tests for the Tier-2 cast remux module (the executor).

Process and disk I/O are mocked: tests never spawn ffmpeg/catt or touch the receiver.
The cast-language *decision* (which track, direct vs remux) lives in `stream_select`
(`vet_cast_audio`) and is tested there; here we test the file-production executor.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from nstream import cast_delivery, caster, remux
from nstream.config import Config
from nstream.tracks import Track, Tracks


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    from nstream import tracks

    monkeypatch.setattr(remux, "_state_path", lambda: tmp_path / "remux.json")
    monkeypatch.setattr(remux, "_cache_dir", lambda: tmp_path)
    # Hermeticity: the free-disk pre-check must not read the HOST's real /tmp usage
    # (a nearly-full tmpfs made the remux refuse and three tests fail). Tests that
    # exercise the guard itself re-stub this with their own value.
    monkeypatch.setattr(remux, "_free_gb", lambda path: 100.0)
    tracks.clear_cache()  # _probe_meta reads the per-url probe memo — keep tests isolated
    yield
    for path in list(remux._PREPARE_LOCKS):
        remux._release_prepare_lock(path)
    tracks.clear_cache()


def _cfg(**kw) -> Config:
    return Config(torrentio_base="tb", **kw)


def test_bridge_meta_kwargs_forwards_app_id():
    meta = caster.CastMeta()
    assert "app_id" not in remux._bridge_meta_kwargs("T", meta, None)
    assert remux._bridge_meta_kwargs("T", meta, None, app_id="CA5T0001")["app_id"] == "CA5T0001"


def _meta(audio=(), n_video=1, duration=0.0):
    """Build a `_probe_meta` return tuple (Tracks, n_video, duration)."""
    return Tracks(audio=list(audio)), n_video, duration


# --- needs_remux / dmr_decodable ---------------------------------------------


@pytest.mark.parametrize("codec", ["ac3", "eac3", "dts", "dtshd", "truehd", "AC3", "TrueHD"])
def test_needs_remux_true_for_dolby_dts(codec):
    assert remux.needs_remux(codec) is True


@pytest.mark.parametrize("codec", ["aac", "opus", "flac", "mp3", "", None])
def test_needs_remux_false_for_decodable(codec):
    assert remux.needs_remux(codec) is False


@pytest.mark.parametrize("codec,ok", [("aac", True), ("opus", True), ("ac3", False), ("", False)])
def test_decodable(codec, ok):
    assert remux.dmr_decodable(codec) is ok


# --- _probe_meta ----------------------------------------------------------


def test_probe_meta_parses_streams(monkeypatch):
    from nstream import util

    payload = {
        "format": {"duration": "1234.5"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc"},
            {
                "codec_type": "audio",
                "codec_name": "EAC3",
                "channels": 6,
                "tags": {"language": "eng"},
            },
        ],
    }

    class _Proc:
        stdout = json.dumps(payload)

    monkeypatch.setattr(util, "run_cmd", lambda *a, **k: _Proc())
    t, n_video, duration = remux._probe_meta("http://x")
    assert n_video == 1
    assert duration == pytest.approx(1234.5)
    assert t.audio[0].codec == "EAC3" and t.audio[0].channels == 6


def test_probe_meta_reuses_the_memoized_probe(monkeypatch):
    """No third ffprobe: a url already probed (audio guard / cast vetting) is a cache hit."""
    from nstream import tracks, util

    payload = {
        "format": {"duration": "10.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc"},
            {"codec_type": "audio", "codec_name": "eac3", "channels": 6},
        ],
    }
    calls = []

    class _Proc:
        stdout = json.dumps(payload)

    def run_cmd(*a, **k):
        calls.append(a)
        return _Proc()

    monkeypatch.setattr(util, "run_cmd", run_cmd)
    tracks.probe_tracks("http://x")  # the earlier probe in the play flow
    t, n_video, duration = remux._probe_meta("http://x")
    assert len(calls) == 1  # _probe_meta did NOT spawn another ffprobe
    assert n_video == 1 and duration == pytest.approx(10.0) and t.audio[0].codec == "eac3"


# --- remux_for_cast -------------------------------------------------------


def test_remux_for_cast_off_when_disabled(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    assert remux.remux_for_cast("http://x", _cfg(cast_remux=False), audio_index=0) is None


def test_remux_for_cast_threads_index_and_meta(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(
        remux, "_probe_meta",
        lambda _u: _meta([Track(id=1, codec="eac3", channels=6)], n_video=2, duration=99.0),
    )  # fmt: skip
    seen = {}

    def fake_remux(url, cfg, *, audio_index, audio, n_video, duration, size_gb, confirm, **_k):
        seen.update(
            audio_index=audio_index, n_video=n_video, duration=duration, size_gb=size_gb,
            confirm=confirm,
        )  # fmt: skip
        return "/tmp/cast-x.mp4"

    monkeypatch.setattr(remux, "remux_to_file", fake_remux)
    ask = lambda q, d: True  # noqa: E731
    out = remux.remux_for_cast("http://x", _cfg(), audio_index=3, size_gb=12.0, confirm=ask)
    assert out == "/tmp/cast-x.mp4"
    assert seen == {
        "audio_index": 3, "n_video": 2, "duration": 99.0, "size_gb": 12.0, "confirm": ask,
    }  # fmt: skip


# --- _audio_bitrate -------------------------------------------------------


@pytest.mark.parametrize(
    "channels,expected", [(None, "192k"), (2, "192k"), (6, "448k"), (8, "640k")]
)
def test_audio_bitrate_scales_with_channels(channels, expected):
    assert remux._audio_bitrate(channels) == expected


# --- remux_to_file --------------------------------------------------------


def _fake_ffmpeg_ok(seen):
    def run(cmd, duration, **_kw):
        seen["cmd"] = cmd
        seen["duration"] = duration
        with open(cmd[-1], "wb") as f:  # ffmpeg writes a non-empty output at the path (last arg)
            f.write(b"x" * 1024)
        return 0, ""

    return run


def test_remux_to_file_encodes_undecodable(monkeypatch, tmp_path):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    seen = {}
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok(seen))
    audio = [Track(id=1, lang="ita", codec="eac3", channels=6)]
    path = remux.remux_to_file("http://x?token=secret", _cfg(), audio_index=0, audio=audio)
    cmd = seen["cmd"]
    assert path and path.endswith(".mp4")
    assert "-c:v" in cmd and "copy" in cmd  # video always copied
    assert "-c:a" in cmd and "aac" in cmd and "448k" in cmd  # 5.1 EAC3 → AAC 448k
    assert "0:a:0?" in cmd  # default audio track
    assert "secret" not in path  # token-bearing url never returned


def test_remux_to_file_copies_decodable_track(monkeypatch, tmp_path):
    # Selecting a non-default AAC track only drops the others → stream-copy, no re-encode.
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    seen = {}
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok(seen))
    audio = [Track(id=1, lang="eng", codec="aac"), Track(id=2, lang="ita", codec="aac")]
    remux.remux_to_file("http://x", _cfg(), audio_index=1, audio=audio)
    cmd = seen["cmd"]
    assert "0:a:1" in cmd  # the requested (Italian) track, audio-relative
    assert cmd[cmd.index("-c:a") + 1] == "copy"  # already decodable → copy
    assert "-b:a" not in cmd


def test_remux_to_file_failure_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda cmd, duration, **k: (1, "boom"))
    assert remux.remux_to_file("http://x", _cfg()) is None
    assert list(tmp_path.glob("cast-*.mp4")) == []  # no leftover temp file


def test_remux_to_file_none_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: False)
    assert remux.remux_to_file("http://x", _cfg()) is None


def test_remux_to_file_aborts_on_insufficient_disk(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 5.0)
    ran = []
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda *a, **k: ran.append(1) or (0, ""))
    assert remux.remux_to_file("http://x", _cfg(), size_gb=40.0) is None
    assert ran == []  # ffmpeg never launched


def test_remux_to_file_size_cap_confirm_declined(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 500.0)
    ran = []
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda *a, **k: ran.append(1) or (0, ""))
    out = remux.remux_to_file(
        "http://x", _cfg(cast_remux_max_size_gb=10), size_gb=30.0, confirm=lambda q, d: False
    )
    assert out is None
    assert ran == []


def test_remux_to_file_size_cap_confirm_accepted(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 500.0)
    seen = {}
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok(seen))
    out = remux.remux_to_file(
        "http://x", _cfg(cast_remux_max_size_gb=10), size_gb=30.0, confirm=lambda q, d: True
    )
    assert out and "cmd" in seen


def test_remux_to_file_size_cap_headless_refuses(monkeypatch):
    # ADR 0037: over the cap only a person may say yes; headless never auto-accepts.
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 500.0)
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda *a, **k: pytest.fail("must not fetch"))
    assert remux.remux_to_file("http://x", _cfg(cast_remux_max_size_gb=10), size_gb=30.0) is None


def test_remux_to_file_unknown_size_refused_on_low_disk(monkeypatch):
    # size_gb=0 (unparsed release size) must NOT skip the free-disk pre-check:
    # below the minimum headroom the remux is refused before ffmpeg launches.
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: remux._MIN_FREE_GB - 1)
    ran = []
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda *a, **k: ran.append(1) or (0, ""))
    assert remux.remux_to_file("http://x", _cfg()) is None
    assert ran == []  # ffmpeg never launched


def test_remux_to_file_unknown_size_proceeds_on_indeterminate_disk(monkeypatch):
    # _free_gb's 0.0 is the "couldn't stat" sentinel, not a full disk: indeterminate
    # must not block the cast (best-effort guard).
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 0.0)
    seen = {}
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok(seen))
    assert remux.remux_to_file("http://x", _cfg()) is not None


def test_remux_to_file_warns_on_dv7_dual_layer(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok({}))
    warns: list[str] = []
    monkeypatch.setattr(remux._log, "warning", lambda msg, *a: warns.append(str(msg)))
    remux.remux_to_file("http://x", _cfg(), n_video=2)
    assert any("dual-layer" in w for w in warns)


# --- _run_ffmpeg ------------------------------------------------------------


def test_run_ffmpeg_drains_big_stderr_without_deadlock():
    """stderr must not be an undrained pipe: a fake ffmpeg writing well past the 64KB pipe
    capacity on stderr would deadlock the stdout progress loop with the old PIPE capture."""
    script = (
        "import sys;"
        "sys.stderr.write('E' * 262144 + 'MARKER');"
        "sys.stdout.write('out_time_us=1000000\\n');"
        "sys.exit(3)"
    )
    rc, stderr = remux._run_ffmpeg([sys.executable, "-c", script], duration=0.0)
    assert rc == 3
    assert stderr.endswith("MARKER")  # full stderr read back for the error message


def test_run_ffmpeg_reports_tenths_when_stderr_is_not_a_tty(capsys, monkeypatch):
    """Headless prepare stays visible: a line at 0% and the next one at 10%, not every percent."""
    script = (
        "import sys;"
        "sys.stdout.write('out_time_us=0\\n');"
        "sys.stdout.write('out_time_us=500000\\n');"
        "sys.stdout.write('out_time_us=1500000\\n');"
        "sys.exit(0)"
    )
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    rc, _stderr = remux._run_ffmpeg(
        [sys.executable, "-c", script], duration=10.0, size_label="~2.2G"
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "  0%" in err and " 10%" in err and "~2.2G" in err
    assert "  5%" not in err


def test_run_ffmpeg_launch_failure_returns_none():
    rc, stderr = remux._run_ffmpeg(["/nonexistent/ffmpeg-xyz"], duration=0.0)
    assert rc is None and stderr


# --- cast_file dispatch (catt fallback path) --------------------------------


class _Proc:
    """A fake detached serving process (catt / `python -m nstream.serve`)."""

    def __init__(self, pid=4242, wait_exc=None, polls_alive=1):
        self.pid = pid
        self.wait_calls = 0
        self.poll_calls = 0
        self._wait_exc = wait_exc
        self._polls_alive = polls_alive

    def wait(self):
        self.wait_calls += 1
        if self._wait_exc:
            raise self._wait_exc

    def poll(self):
        """None while "alive" for the first `polls_alive` checks, then 0 (exited).
        `wait_exc` raises here too — models Ctrl-C during the follow poll loop."""
        self.poll_calls += 1
        if self._wait_exc:
            raise self._wait_exc
        return None if self.poll_calls <= self._polls_alive else 0


def _catt_wiring(monkeypatch, *, await_start=True, proc=None):
    """Wire cast_file's catt path to fakes: no firewall/sudo, no network, a recording
    Popen/run/_kill, and a stubbed receiver-start poll. bridge_available is already
    False (conftest), so cast_file goes straight to the catt fallback."""
    rec = {"popen": [], "run": [], "killed": []}
    proc = proc or _Proc()
    monkeypatch.setattr(remux.serve, "ensure_firewall", lambda ip: None)
    monkeypatch.setattr(remux.serve, "lan_ip", lambda ip: "192.168.1.10")
    monkeypatch.setattr(remux.serve, "firewall_hint", lambda ip, port=None: "FIREWALL-HINT")
    monkeypatch.setattr(remux, "_await_start", lambda dev: await_start)
    monkeypatch.setattr(remux, "_kill", lambda pid: rec["killed"].append(pid))
    monkeypatch.setattr(remux, "_STATUS_POLL", 0.0)  # the follow loop must not sleep 15s
    monkeypatch.setattr(
        remux.caster,
        "status",
        lambda dev: {
            "player_state": "IDLE",
            "title": None,
            "position": 0.0,
            "duration": 0.0,
            "volume": None,
            "muted": False,
        },
    )

    def popen(args, **kw):
        rec["popen"].append((list(args), kw))
        return proc

    monkeypatch.setattr(remux.subprocess, "Popen", popen)
    monkeypatch.setattr(
        remux.subprocess, "run", lambda cmd, **kw: rec["run"].append(list(cmd)) or _P(0)
    )
    return rec, proc


def _tmp_remux(tmp_path):
    f = tmp_path / "cast-x.mp4"
    f.write_bytes(b"x")
    return f


def test_cast_file_prefers_bridge_result_over_catt(monkeypatch, tmp_path):
    """castbridge present and started → its result is returned and catt never launches;
    the firewall pre-open covers the bridge-served path too."""
    f = _tmp_remux(tmp_path)
    fw: list[str] = []
    monkeypatch.setattr(remux.serve, "ensure_firewall", lambda ip: fw.append(ip))
    monkeypatch.setattr(remux.serve, "lan_ip", lambda ip: "192.168.1.10")
    monkeypatch.setattr(remux.bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(
        remux, "_cast_file_via_bridge",
        lambda *a, **k: remux.cast_delivery.CastResult(5.0, 99.0, False, started=True),
    )  # fmt: skip
    monkeypatch.setattr(
        remux.subprocess, "Popen", lambda *a, **k: pytest.fail("catt fallback launched")
    )
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5")
    assert (out.pos, out.dur, out.subs_delivered) == (5.0, 99.0, False)
    assert fw == ["192.168.1.10"]


def test_cast_file_bridge_none_falls_back_to_catt(monkeypatch, tmp_path):
    """castbridge available but unable to start (None) → detached catt serves+casts."""
    f = _tmp_remux(tmp_path)
    rec, _proc = _catt_wiring(monkeypatch)
    monkeypatch.setattr(remux.bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(remux, "_cast_file_via_bridge", lambda *a, **k: None)
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=False)
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, False)
    assert rec["popen"][0][0][:4] == ["catt", "-d", "10.0.0.5", "cast"]


def test_cast_file_catt_headless_detaches_and_keeps_state(monkeypatch, tmp_path):
    """follow=False: the detached catt keeps serving (not killed, not waited), the state
    file records pid/file/device in catt mode for --stop/GC, argv carries -t/-s."""
    f = _tmp_remux(tmp_path)
    rec, proc = _catt_wiring(monkeypatch)
    out = remux.cast_file(
        _cfg(), "T", str(f), device="10.0.0.5",
        start=30.0, sub_paths=("/s.srt",), follow=False,
    )  # fmt: skip
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, True)
    args, kw = rec["popen"][0]
    assert args == ["catt", "-d", "10.0.0.5", "cast", str(f), "-t", "30", "-s", "/s.srt"]
    assert kw["start_new_session"] is True  # detached: outlives the headless return
    assert remux._read_state() == {
        "pid": 4242, "file": str(f), "device": "10.0.0.5", "mode": "catt",
    }  # fmt: skip
    assert proc.wait_calls == 0 and rec["killed"] == []
    assert f.exists()
    assert list(tmp_path.glob("catt-*.log")) == []  # diagnosis capture removed on startup


def test_cast_file_catt_follow_waits_then_tears_down(monkeypatch, tmp_path):
    f = _tmp_remux(tmp_path)
    rec, proc = _catt_wiring(monkeypatch)
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=True)
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, False)
    assert proc.poll_calls >= 1  # followed until the serving catt exited
    assert rec["killed"] == [4242]  # _teardown reaps the serving catt
    assert not f.exists()
    assert remux._read_state() is None


def test_cast_file_catt_follow_ctrl_c_stops_receiver_and_tears_down(monkeypatch, tmp_path):
    """Ctrl-C while following a catt cast: receiver stopped, full teardown, and the
    call returns normally (the KeyboardInterrupt is swallowed on this path —
    photographed; the bridge path re-raises instead)."""
    f = _tmp_remux(tmp_path)
    rec, _proc = _catt_wiring(monkeypatch, proc=_Proc(wait_exc=KeyboardInterrupt()))
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=True)
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, False)
    assert ["catt", "-d", "10.0.0.5", "stop"] in rec["run"]
    assert rec["killed"] == [4242]
    assert not f.exists() and remux._read_state() is None


def test_cast_file_catt_missing_cleans_temp(monkeypatch, tmp_path, capsys):
    f = _tmp_remux(tmp_path)
    monkeypatch.setattr(remux.serve, "ensure_firewall", lambda ip: None)
    monkeypatch.setattr(remux.serve, "lan_ip", lambda ip: "192.168.1.10")

    def boom(*a, **k):
        raise FileNotFoundError("catt")

    monkeypatch.setattr(remux.subprocess, "Popen", boom)
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5")
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, False)
    assert not f.exists()  # temp not leaked
    assert list(tmp_path.glob("catt-*.log")) == []  # stderr capture not leaked
    assert "catt non trovato" in capsys.readouterr().err


def test_cast_file_start_timeout_tears_down_with_firewall_hint(monkeypatch, tmp_path, capsys):
    """Receiver never starts playing the served file → kill the catt, remove the temp,
    clear the state, and print the firewall hint (the most common cause)."""
    f = _tmp_remux(tmp_path)
    rec, _proc = _catt_wiring(monkeypatch, await_start=False)
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=False)
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, False)
    assert out.started is False and out.error == "cast_never_started"
    assert rec["killed"] == [4242]
    assert not f.exists()
    assert remux._read_state() is None
    assert "FIREWALL-HINT" in capsys.readouterr().err


# --- _cast_file_via_bridge --------------------------------------------------


class _FakeServer:
    def __init__(self):
        self.down = False
        self.token = "tok"  # per-cast capability token (serve.py URL hardening)

    def shutdown(self):
        self.down = True


def _bridge_scaffold(monkeypatch, cast_load):
    """Wire cast_file's bridge path to fakes: castbridge present, no firewall/network,
    a recording Range server, the given `cast_load` generator, a recording bridge.stop,
    and a catt fallback that fails the test if launched."""
    srv = _FakeServer()
    stopped: list[str | None] = []
    monkeypatch.setattr(remux.bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(remux.bridge, "cast_load", cast_load)
    monkeypatch.setattr(remux.bridge, "stop", lambda dev=None: stopped.append(dev) or True)
    monkeypatch.setattr(remux.serve, "ensure_firewall", lambda ip: None)
    monkeypatch.setattr(remux.serve, "lan_ip", lambda ip: "192.168.1.10")
    monkeypatch.setattr(remux.serve, "serve_file", lambda p, b, sub_path=None: (srv, 46000, None))
    monkeypatch.setattr(
        remux.subprocess, "Popen",
        lambda *a, **k: pytest.fail("catt fallback launched"),
    )  # fmt: skip
    return srv, stopped


def test_cast_file_ctrl_c_before_start_reraises_no_catt_fallback(monkeypatch, tmp_path):
    """Ctrl-C during the startup wait is a user abort, not a bridge failure: it must
    propagate (clean exit 130 upstream), tear down, and never degrade to a detached
    catt re-casting the file the user just cancelled."""
    f = tmp_path / "cast-x.mp4"
    f.write_bytes(b"x")

    def aborted(*a, **k):
        raise KeyboardInterrupt  # Ctrl-C while waiting for `started`
        yield  # pragma: no cover — make it a generator

    srv, stopped = _bridge_scaffold(monkeypatch, aborted)
    with pytest.raises(KeyboardInterrupt):
        remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=True)
    assert stopped == ["10.0.0.5"]  # receiver session stopped
    assert not f.exists()  # no fallback will use the temp → removed
    assert srv.down  # Range server shut down


def test_cast_file_bridge_disconnect_keeps_file_and_server(monkeypatch, tmp_path):
    """A daemon `disconnected` mid-cast is NOT a playback end: the TV is still fetching
    from the Range server, so neither the server nor the temp file may be torn down
    (cleanup is the next run's _gc_stale / --stop)."""
    f = tmp_path / "cast-x.mp4"
    f.write_bytes(b"x")

    def crashed(*a, **k):
        yield {"kind": "started", "title": "T"}
        yield {"kind": "playing", "position": 10.0, "duration": 100.0}
        yield {"kind": "disconnected", "position": 42.0, "duration": 100.0}

    srv, stopped = _bridge_scaffold(monkeypatch, crashed)
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=True)
    assert (out.pos, out.dur, out.subs_delivered) == (
        42.0,
        100.0,
        False,
    )  # last known position reported, no subs
    assert f.exists()  # temp left for the still-streaming receiver
    assert not srv.down  # Range server kept serving
    assert stopped == []  # the receiver session is NOT stopped


def test_cast_file_bridge_follow_success_cleans_up(monkeypatch, tmp_path):
    """Happy path: in-process Range server + castbridge events; a cast that ran to its
    end reports the last position, shuts the server down and removes the temp file."""
    f = _tmp_remux(tmp_path)

    def played(*a, **k):
        yield {"kind": "started", "title": "T"}
        yield {"kind": "playing", "position": 10.0, "duration": 100.0}
        yield {"kind": "ended", "position": 95.0, "duration": 100.0}

    srv, stopped = _bridge_scaffold(monkeypatch, played)
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=True)
    assert (out.pos, out.dur, out.subs_delivered) == (95.0, 100.0, False)
    assert not f.exists()  # completed cast → temp removed
    assert srv.down  # Range server shut down
    assert stopped == []  # session ended by itself, no explicit stop


def test_cast_file_headless_bridge_writes_serve_state(monkeypatch, tmp_path):
    """follow=False on the bridge path: detached `nstream.serve` + castbridge LOAD;
    the state records the server pid in `serve` mode so stop() tears down the right
    receiver session (bridge.stop, not catt stop)."""
    f = _tmp_remux(tmp_path)

    def started(*a, **k):
        yield {"kind": "started", "title": "T"}

    _srv, _stopped = _bridge_scaffold(monkeypatch, started)
    monkeypatch.setattr(
        remux.serve,
        "spawn_detached",
        lambda bind_ip, file_path=None, sub_path=None: (777, 46001, "tok"),
    )
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=False)
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, False)
    assert remux._read_state() == {
        "pid": 777, "file": str(f), "device": "10.0.0.5", "mode": "serve",
    }  # fmt: skip
    assert f.exists()  # detached server still serving it


def test_cast_file_headless_bridge_failed_falls_back_to_catt(monkeypatch, tmp_path):
    """LOAD failed before `started` (follow=False): the spawned Range server is killed,
    the temp file is KEPT, and cast_file degrades to the detached catt."""
    f = _tmp_remux(tmp_path)
    rec, _proc = _catt_wiring(monkeypatch)
    monkeypatch.setattr(remux.bridge, "bridge_available", lambda: True)

    def failed(*a, **k):
        yield {"kind": "failed", "error": "load_failed", "message": "nope"}

    monkeypatch.setattr(remux.bridge, "cast_load", failed)
    monkeypatch.setattr(
        remux.serve,
        "spawn_detached",
        lambda bind_ip, file_path=None, sub_path=None: (777, 46001, "tok"),
    )
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=False)
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, False)
    assert 777 in rec["killed"]  # bridge-path server reaped before the fallback
    args, _kw = rec["popen"][0]
    assert args[:5] == ["catt", "-d", "10.0.0.5", "cast", str(f)]  # same temp re-served
    stored = remux._read_state()
    assert stored is not None and stored["mode"] == "catt"


def test_cast_file_headless_ctrl_c_propagates_no_catt_fallback(monkeypatch, tmp_path):
    """Ctrl-C during the headless startup wait propagates (no catt re-cast of the
    cancelled file) AND tears down like the follow path: receiver session stopped,
    the already-spawned detached server reaped (it has no state yet, so --stop could
    never find it), and the temp file removed (no fallback will use it)."""
    f = _tmp_remux(tmp_path)

    def aborted(*a, **k):
        raise KeyboardInterrupt
        yield  # pragma: no cover — make it a generator

    _srv, stopped = _bridge_scaffold(monkeypatch, aborted)
    killed: list[int] = []
    monkeypatch.setattr(remux, "_kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(
        remux.serve,
        "spawn_detached",
        lambda bind_ip, file_path=None, sub_path=None: (777, 46001, "tok"),
    )
    with pytest.raises(KeyboardInterrupt):
        remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=False)
    assert stopped == ["10.0.0.5"]  # receiver session stopped
    assert killed == [777]  # detached server reaped (no orphan invisible to --stop)
    assert not f.exists()  # temp removed — no fallback will use it
    assert remux._read_state() is None


# --- _spawn_server ----------------------------------------------------------


class _ServeProc:
    def __init__(self, line, pid=777):
        self.pid = pid
        self.stdout = io.StringIO(line)


def test_spawn_server_returns_pid_port_and_token(monkeypatch):
    monkeypatch.setattr(
        remux.serve.subprocess, "Popen", lambda *a, **k: _ServeProc("PORT=46001\nTOKEN=abc\n")
    )
    assert remux.serve.spawn_detached("192.168.1.10", file_path="/f.mp4") == (777, 46001, "abc")


def test_spawn_server_kills_on_missing_token(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(remux.serve, "kill_detached", lambda pid: killed.append(pid))
    monkeypatch.setattr(remux.serve.subprocess, "Popen", lambda *a, **k: _ServeProc("PORT=46001\n"))
    assert remux.serve.spawn_detached("192.168.1.10", file_path="/f.mp4") is None
    assert killed == [777]  # no orphan server when the token never arrives


def test_spawn_server_kills_on_bad_announcement(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr(remux.serve, "kill_detached", lambda pid: killed.append(pid))
    monkeypatch.setattr(remux.serve.subprocess, "Popen", lambda *a, **k: _ServeProc("boom\n"))
    assert remux.serve.spawn_detached("192.168.1.10", file_path="/f.mp4") is None
    assert killed == [777]  # no orphan server on a botched handshake


# --- _await_start -----------------------------------------------------------


def test_await_start_true_once_receiver_plays(monkeypatch):
    states = iter([None, {"player_state": "BUFFERING"}])
    monkeypatch.setattr(remux.caster, "receiver_info", lambda dev: next(states))
    monkeypatch.setattr(remux.time, "sleep", lambda s: None)
    assert remux._await_start("10.0.0.5") is True


def test_await_start_false_when_receiver_stays_idle(monkeypatch):
    """Timeout path: the states observed are logged so the failure is reconstructable."""
    monkeypatch.setattr(remux.caster, "receiver_info", lambda dev: {"player_state": "IDLE"})
    monkeypatch.setattr(remux, "_START_TIMEOUT", 0.02)
    monkeypatch.setattr(remux, "_START_POLL", 0.0)
    warns: list[str] = []
    monkeypatch.setattr(remux._log, "warning", lambda msg, *a: warns.append(msg % a))
    assert remux._await_start("10.0.0.5") is False
    assert any("IDLE" in w for w in warns)


# --- _gc_stale --------------------------------------------------------------


def test_gc_stale_keeps_only_the_live_serve_file(tmp_path):
    keep = tmp_path / "cast-live.mp4"
    keep.write_bytes(b"x")
    stale = tmp_path / "cast-old.mp4"
    stale.write_bytes(b"x")
    logf = tmp_path / "catt-old.log"
    logf.write_text("e")
    (tmp_path / "remux.json").write_text(json.dumps({"pid": os.getpid(), "file": str(keep)}))
    remux._gc_stale()
    assert keep.exists()  # its server (this test process) is alive
    assert not stale.exists() and not logf.exists()


def test_gc_stale_removes_all_when_server_dead(tmp_path, monkeypatch):
    f = tmp_path / "cast-dead.mp4"
    f.write_bytes(b"x")
    (tmp_path / "remux.json").write_text(json.dumps({"pid": 12345, "file": str(f)}))
    monkeypatch.setattr(remux, "_pid_alive", lambda pid: False)
    remux._gc_stale()
    assert not f.exists()


def test_gc_stale_keeps_in_progress_remux_until_server_state_exists(tmp_path):
    """A concurrent status command must not unlink the output under a running ffmpeg."""
    path = remux._new_remux_temp()
    f = tmp_path / Path(path).name
    f.write_bytes(b"partial")

    remux._gc_stale()

    assert f.exists()
    assert Path(f"{path}.lock").exists()


def test_new_remux_temp_lock_is_held_before_it_is_visible(monkeypatch, tmp_path):
    """A GC racing the creation (here: run right before the lock is published) must find
    nothing it may delete — the lock was visible-but-unlocked in the old mkstemp order."""
    real_link = os.link

    def racing_link(src, dst):
        remux._gc_stale()
        return real_link(src, dst)

    monkeypatch.setattr(remux.os, "link", racing_link)
    path = remux._new_remux_temp()
    remux._gc_stale()  # and one after: the published lock is held
    assert Path(path).exists() and Path(f"{path}.lock").exists()
    assert list(tmp_path.glob(".cast-*")) == []  # the hidden name is gone


def test_gc_removes_unpublished_lock_of_a_crashed_run(tmp_path):
    (tmp_path / ".cast-dead.mp4.lock").write_bytes(b"")
    remux._gc_stale()
    assert not (tmp_path / ".cast-dead.mp4.lock").exists()


def test_write_state_atomically_replaces_prepare_lock(tmp_path):
    path = remux._new_remux_temp()
    f = tmp_path / Path(path).name
    f.write_bytes(b"complete")

    remux._write_state(os.getpid(), path, "TV", mode="serve")
    remux._gc_stale()

    assert f.exists()
    assert not Path(f"{path}.lock").exists()


# --- stop / state ---------------------------------------------------------


class _P:
    def __init__(self, rc=0, stderr=""):
        self.returncode = rc
        self.stderr = stderr


def test_stop_no_state_returns_false():
    assert remux.stop("Salotto") is False


def test_stop_tears_down_tracked_server(monkeypatch, tmp_path):
    f = tmp_path / "cast-abc.mp4"
    f.write_bytes(b"x")
    (tmp_path / "remux.json").write_text(json.dumps({"pid": 4242, "file": str(f), "device": "TV"}))
    killed, stopped = [], []
    monkeypatch.setattr(remux, "_kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(remux.subprocess, "run", lambda cmd, **kw: stopped.append(cmd) or _P(0))
    assert remux.stop() is True
    assert killed == [4242]
    assert not f.exists()  # temp removed
    assert not (tmp_path / "remux.json").exists()  # state cleared
    assert any("stop" in c for c in stopped)  # receiver stopped


# --- M3/S4/L3: srt sidecar, previous-server reap, Ctrl-C temp cleanup -------


def test_cast_file_detached_copies_srt_out_of_workdir(monkeypatch, tmp_path):
    """follow=False: the subtitle lives in the caller's dying temp dir → converted to our
    cleaned WebVTT, copied beside the remux (same GC/teardown lifecycle) and catt is
    pointed at the copy (catt's own SRT reader mangles CP1252 text)."""
    f = _tmp_remux(tmp_path)
    sub = tmp_path / "workdir-sub.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nciao\n")
    rec, _proc = _catt_wiring(monkeypatch)
    out = remux.cast_file(
        _cfg(), "T", str(f), device="10.0.0.5", sub_paths=(str(sub),), follow=False
    )
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, True)
    sidecar = tmp_path / "cast-x.mp4.vtt"
    assert sidecar.exists() and sidecar.read_text().startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.000\nciao" in sidecar.read_text()
    args, _kw = rec["popen"][0]
    assert args[-2:] == ["-s", str(sidecar)]


def test_teardown_removes_srt_sidecar(monkeypatch, tmp_path):
    f = _tmp_remux(tmp_path)
    sidecar = tmp_path / "cast-x.mp4.srt"
    sidecar.write_text("x")
    monkeypatch.setattr(remux, "_kill", lambda pid: None)
    remux._teardown(None, str(f))
    assert not f.exists() and not sidecar.exists()


def test_cast_file_reaps_previous_detached_server(monkeypatch, tmp_path):
    """The state slot is single: a second cast must tear down the first detached
    server instead of orphaning it (it would listen and hold its temp forever)."""
    old = tmp_path / "cast-old.mp4"
    old.write_bytes(b"old")
    remux._write_state(999999, str(old), "10.0.0.5")
    monkeypatch.setattr(remux, "_pid_alive", lambda pid: pid == 999999)
    f = _tmp_remux(tmp_path)
    rec, _proc = _catt_wiring(monkeypatch)
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=False)
    assert (out.pos, out.dur, out.subs_delivered) == (0.0, 0.0, False)
    assert 999999 in rec["killed"] and not old.exists()
    st = remux._read_state() or {}
    assert st is not None
    assert st["file"] == str(f)  # slot now owned by the new cast


def test_remux_to_file_ctrl_c_removes_partial(monkeypatch, tmp_path):
    """L3: an aborted prepare must not leak the partial multi-GB temp."""
    monkeypatch.setattr(remux, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_free_gb", lambda p: 1000.0)

    def interrupted(cmd, duration, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(remux, "_run_ffmpeg", interrupted)
    with pytest.raises(KeyboardInterrupt):
        remux.remux_to_file("http://u", _cfg())
    assert list(tmp_path.glob("cast-*.mp4")) == []


def test_cast_file_follow_catt_reports_progress(monkeypatch, tmp_path):
    """The catt follow branch must honour the (position, duration, …) contract like
    caster.cast — without the poll a Tier-2 --follow cast left no resume point."""
    f = _tmp_remux(tmp_path)
    rec, proc = _catt_wiring(monkeypatch, proc=_Proc(polls_alive=2))
    positions = iter([(300.0, 5000.0), (1200.0, 5000.0)])

    def status(dev):
        pos, dur = next(positions, (0.0, 0.0))
        return {
            "player_state": "PLAYING",
            "title": "T",
            "position": pos,
            "duration": dur,
            "volume": None,
            "muted": False,
        }

    monkeypatch.setattr(remux.caster, "status", status)
    out = remux.cast_file(_cfg(), "T", str(f), device="10.0.0.5", follow=True)
    assert (out.pos, out.dur, out.subs_delivered) == (
        1200.0,
        5000.0,
        False,
    )  # last known position wins, no subs
    assert rec["killed"] == [4242] and not f.exists()  # teardown unchanged


def test_run_ffmpeg_abort_terminates_child(monkeypatch):
    # Ctrl-C mid-prepare: the child must be stopped before the caller deletes the partial
    # file, or it keeps downloading into an unlinked inode.
    import subprocess as sp
    import sys

    spawned: list = []
    real_popen = sp.Popen

    def popen(*a, **k):
        proc = real_popen(*a, **k)
        spawned.append(proc)
        return proc

    def interrupt(_msg):
        raise KeyboardInterrupt

    monkeypatch.setattr(remux.subprocess, "Popen", popen)
    monkeypatch.setattr(remux.ui, "progress", interrupt)
    with pytest.raises(KeyboardInterrupt):
        remux._run_ffmpeg([sys.executable, "-c", "import time; time.sleep(30)"], 10.0)
    assert spawned and spawned[0].poll() is not None


def test_remux_cmd_bounds_stalled_source(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 500.0)
    monkeypatch.setattr(remux, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda cmd, d, **k: seen.append(cmd) or (1, "x"))
    remux.remux_to_file("http://src", Config(torrentio_base="tb"), size_gb=1.0)
    cmd = seen[0]
    assert cmd[cmd.index("-rw_timeout") + 1] == "30000000"
    assert cmd.index("-rw_timeout") < cmd.index("-i")


def test_remux_argv_never_carries_the_debrid_url(monkeypatch, tmp_path):
    seen: list = []
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 500.0)
    monkeypatch.setattr(remux, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda cmd, d, **k: seen.append(cmd) or (1, "x"))
    remux.remux_to_file("https://rd.example/realdebrid=TOK/f.mkv", Config(torrentio_base="tb"))
    assert not any("TOK" in a for a in seen[0])


# --- live HLS-TS (ADR 0039) ---------------------------------------------------


def _live_wiring(monkeypatch, *, writes_segments: bool):
    from nstream import live, tracks

    seen: dict = {}
    monkeypatch.setattr(remux.bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "live_refusal", lambda *a: None)
    monkeypatch.setattr(remux.serve, "lan_ip", lambda d: "127.0.0.1")
    monkeypatch.setattr(remux.serve, "ensure_firewall", lambda ip: None)
    monkeypatch.setattr(remux, "_LIVE_STALL_S", 0.3)
    monkeypatch.setattr(
        remux, "_probe_meta",
        lambda url: (tracks.Tracks(audio=[tracks.Track(0, "ita", "ac3", channels=6)]), 1, 6000.0),
    )  # fmt: skip

    def spawn(bind_ip, *, sub_path=None, hls_dir=None, job=None, file_path=None):
        seen["job"] = job
        if writes_segments:
            subs = bool(job and job.subs)
            lines = ["#EXTM3U"] + [
                f"#EXTINF:6.0,\n{live.segment_name(0, i, subs)}" for i in range(3)
            ]
            Path(str(hls_dir), live.playlist_name(0, subs)).write_text("\n".join(lines) + "\n")
        seen["dir"] = hls_dir
        return 4242, 45001, "tok"

    monkeypatch.setattr(remux.serve, "spawn_detached", spawn)
    alive = {"v": True}
    monkeypatch.setattr(remux, "_pid_alive", lambda pid: alive["v"] and pid == 4242)
    monkeypatch.setattr(remux, "_kill", lambda pid: alive.update(v=False))

    def drive(device, url, *, follow, load_kwargs, **k):
        seen["url"], seen["kwargs"] = url, load_kwargs
        return cast_delivery.BridgeOutcome(0.0, 0.0, True, False)

    monkeypatch.setattr(remux.cast_delivery, "drive_bridge", drive)
    return seen


def test_cast_live_detached_loads_the_playlist_and_records_state(monkeypatch):
    seen = _live_wiring(monkeypatch, writes_segments=True)
    out = remux.cast_live(
        _cfg(), "T", "http://debrid/x", device="10.0.0.5", audio_index=0, start=0.0, follow=False
    )
    assert out is not None and out.started
    assert seen["url"].endswith("/hls/index.m3u8")
    assert seen["kwargs"]["content_type"] == "application/vnd.apple.mpegurl"
    assert seen["job"].audio_args[:2] == ("-c:a", "aac") and "-ac" in seen["job"].audio_args
    assert remux._read_state() == {
        "pid": 4242, "file": seen["dir"], "device": "10.0.0.5", "mode": "live",
        "url": seen["url"], "title": "T", "base": 0.0, "duration": 6000.0,
        "subs": False, "text_language": "",
    }  # fmt: skip


def test_cast_live_without_segments_cleans_up_and_yields(monkeypatch):
    """No segment in time → None (the caller falls back to the complete file), the detached
    server is killed and the directory removed."""
    seen = _live_wiring(monkeypatch, writes_segments=False)
    out = remux.cast_live(
        _cfg(), "T", "http://debrid/x", device="10.0.0.5", audio_index=0, follow=False
    )
    assert out is None
    assert not Path(seen["dir"]).exists()
    assert remux._read_state() is None


def test_live_job_copies_stereo_aac_and_downmixes_the_rest():
    from nstream import tracks

    aac = [tracks.Track(0, "ita", "aac", channels=2)]
    assert remux._live_job("u", _cfg(), aac, 0, 0.0).audio_args == ("-c:a", "copy")
    eac3 = [tracks.Track(0, "eng", "aac", channels=6), tracks.Track(1, "ita", "eac3", channels=6)]
    job = remux._live_job("u", _cfg(), eac3, 1, 90.0)
    assert job.audio_map == "0:a:1" and job.head_s == 90.0
    assert job.audio_args == ("-c:a", "aac", "-ac", "2", "-b:a", "192k")


def test_cast_live_ignores_the_custom_receiver(monkeypatch):
    """The custom receiver refuses HLS (LOAD_FAILED, field 2026-10-01): live always LOADs
    on the Default Media Receiver."""
    seen = _live_wiring(monkeypatch, writes_segments=True)
    remux.cast_live(
        _cfg(cast_receiver_app_id="CA5T0001"), "T", "http://debrid/x",
        device="10.0.0.5", audio_index=0, follow=False,
    )  # fmt: skip
    assert "app_id" not in seen["kwargs"]


def _live_state(monkeypatch, tmp_path, *, pos: float, produced: int = 100):
    from nstream import live

    d = tmp_path / "cast-x.hls"
    d.mkdir()
    lines = ["#EXTM3U"] + [f"#EXTINF:10.0,\nindex{i}.ts" for i in range(produced)]
    (d / live.PLAYLIST).write_text("\n".join(lines) + "\n")
    remux._write_state(4242, str(d), "10.0.0.5", mode="live", url="http://h/pl.m3u8", title="T")
    monkeypatch.setattr(remux, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(remux.caster, "status", lambda dev: {"position": pos})
    loads: list = []

    def load(dev, url, *, follow, **kw):
        loads.append((url, kw["current_time"], kw["content_type"]))
        yield {"kind": "started"}

    monkeypatch.setattr(remux.bridge, "cast_load", load)
    return loads


def test_live_seek_reloads_far_jumps(monkeypatch, tmp_path):
    """The receiver clamps a far seek on a live playlist (+180 → +5, field 2026-10-01):
    the playlist is LOADed again at the target instead."""
    loads = _live_state(monkeypatch, tmp_path, pos=600.0)
    assert remux.live_seek("10.0.0.5", 900.0) is True
    assert loads == [("http://h/pl.m3u8", 900.0, "application/vnd.apple.mpegurl")]


def test_live_seek_short_jumps_native_far_beyond_reach_restarts(monkeypatch, tmp_path):
    _live_state(monkeypatch, tmp_path, pos=600.0, produced=80)  # 800 s produced
    assert remux.live_seek("10.0.0.5", 620.0) is None  # short, no offset: the receiver's seek
    restarts = []
    monkeypatch.setattr(remux, "_live_restart", lambda st, dev, t: restarts.append(t) or True)
    assert remux.live_seek("10.0.0.5", 5000.0) is True  # 73 min ahead: past the pacing reach
    assert restarts == [5000.0]


def test_live_seek_before_the_playlist_start_restarts(monkeypatch, tmp_path):
    """A fast resume at 50 min, then a seek back to 20 min: nothing was produced there."""
    _live_state(monkeypatch, tmp_path, pos=3100.0)
    st = remux._read_state() or {}
    remux._write_state(4242, st["file"], "10.0.0.5", mode="live", url=st["url"], base=3000.0)
    restarts = []
    monkeypatch.setattr(remux, "_live_restart", lambda st, dev, t: restarts.append(t) or True)
    assert remux.live_seek("10.0.0.5", 1200.0) is True and restarts == [1200.0]


def test_live_restart_requests_a_generation_and_loads_it(monkeypatch, tmp_path):
    from nstream import live

    loads = _live_state(monkeypatch, tmp_path, pos=3100.0)
    st = remux._read_state() or {}
    d = Path(st["file"])
    sent = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        req = json.loads((d / remux.serve.RESTART_REQUEST).read_text())
        lines = ["#EXTM3U"] + [f"#EXTINF:6.0,\ng{req['gen']}_{i}.ts" for i in range(3)]
        (d / live.playlist_name(req["gen"])).write_text("\n".join(lines) + "\n")

    monkeypatch.setattr(remux.os, "kill", fake_kill)
    monkeypatch.setattr(remux.live, "first_pts", lambda out, gen, subs=False: 1197.5)
    assert remux._live_restart(st, "10.0.0.5", 1200.0) is True
    assert sent and sent[0][1] == remux.signal.SIGUSR1
    new = remux._read_state() or {}
    assert new["gen"] == 1 and new["base"] == 1197.5 and new["url"].endswith("/g1.m3u8")
    assert loads[-1] == ("http://h/g1.m3u8", 1200.0, "application/vnd.apple.mpegurl")


def test_live_seek_is_none_without_a_live_cast(monkeypatch):
    assert remux.live_seek("10.0.0.5", 900.0) is None


def test_cast_live_fast_resume_starts_the_producer_at_the_resume_point(monkeypatch, tmp_path):
    """Resume at 50 min used to produce 50 min first (223 s, field 2026-10-01). Now the
    producer opens the source there; the LOAD seeks to the resume point in film time (the
    served playlist starts with a gap up to `base`). The downloaded subtitle is the
    producer's second input (a rendition, ADR 0042), never a side-loaded track."""
    seen = _live_wiring(monkeypatch, writes_segments=True)
    monkeypatch.setattr(remux.live, "first_pts", lambda d, gen=0, subs=False: 3027.125)
    sub = tmp_path / "s.srt"
    sub.write_text("1\n00:50:30,000 --> 00:50:32,000\nciao\n")
    out = remux.cast_live(
        _cfg(), "T", "http://debrid/x", device="10.0.0.5", audio_index=0, start=3030.5,
        sub_paths=(str(sub),), sub_lang="ita", follow=False,
    )  # fmt: skip
    assert out is not None and out.started and out.subs_delivered
    job = seen["job"]
    assert job.ss_s == 3030.5 and job.head_s == 0.0
    assert job.sub_map == "1:0" and job.sub_lang == "ita" and job.rms
    assert Path(job.sub_file).parent == Path(seen["dir"])
    assert "00:50:30.000 --> 00:50:32.000" in Path(job.sub_file).read_text()
    assert seen["kwargs"]["current_time"] == 3030.5
    assert seen["kwargs"]["text_language"] == "it" and "subtitle_url" not in seen["kwargs"]
    assert seen["url"].endswith("/hls/s0master.m3u8")
    assert (remux._read_state() or {})["base"] == 3027.125


def test_live_seek_loads_film_time_against_the_playlist_base(monkeypatch, tmp_path):
    loads = _live_state(monkeypatch, tmp_path, pos=3100.0)
    st = remux._read_state() or {}
    remux._write_state(4242, st["file"], "10.0.0.5", mode="live", url=st["url"], base=3000.0)
    assert remux.live_seek("10.0.0.5", 3500.0) is True  # 500 s into a 1000 s playlist
    assert loads[-1][1] == 3500.0
    assert remux.live_seek("10.0.0.5", 3110.0) is None  # short: the receiver's own seek


def test_cast_live_follow_events_carry_the_probed_duration(monkeypatch, tmp_path):
    """--follow JSONL: a growing playlist reports duration -1; events carry the runtime."""
    _live_wiring(monkeypatch, writes_segments=True)
    events: list = []

    def drive(device, url, *, follow, load_kwargs, on_event=None, **k):
        assert on_event is not None
        on_event({"kind": "playing", "position": 3012.0, "duration": -1.0})
        return cast_delivery.BridgeOutcome(3012.0, -1.0, True, False)

    monkeypatch.setattr(remux.cast_delivery, "drive_bridge", drive)
    remux.cast_live(
        _cfg(), "T", "http://debrid/x", device="10.0.0.5", audio_index=0, start=3001.0,
        follow=False, on_event=events.append,
    )  # fmt: skip
    assert events == [{"kind": "playing", "position": 3012.0, "duration": 6000.0}]


def test_report_rate_records_the_link_and_warns_when_slow(monkeypatch):
    recorded = []
    monkeypatch.setattr(remux.state_throughput, "record", lambda h, b: recorded.append((h, b)))
    with remux.notices.capture() as bag:
        remux._report_rate("http://abc.download.real-debrid.com/x", {"rate": 0.9, "bps": 2.4e6})
        remux._report_rate("http://abc.download.real-debrid.com/x", {"rate": 8.0, "bps": 2.4e6})
    assert recorded == [("real-debrid.com", 2.4e6)] * 2
    assert [n.code for n in bag] == ["live_slow"]


def test_cast_live_embedded_subtitles_load_the_master_and_activate_by_language(monkeypatch):
    """ADR 0042: an embedded text track rides as a rendition; the LOAD is the master and
    castbridge activates it by language (field 2026-10-02: DEFAULT=YES alone does not)."""
    seen = _live_wiring(monkeypatch, writes_segments=False)

    def spawn(bind_ip, *, sub_path=None, hls_dir=None, job=None, file_path=None):
        from nstream import live

        seen["job"], seen["dir"], seen["sub_path"] = job, hls_dir, sub_path
        lines = ["#EXTM3U"] + [f"#EXTINF:6.0,\ns0v0_{i}.ts" for i in range(3)]
        Path(str(hls_dir), live.playlist_name(0, subs=True)).write_text("\n".join(lines) + "\n")
        return 4242, 45001, "tok"

    monkeypatch.setattr(remux.serve, "spawn_detached", spawn)

    def drive(device, url, *, follow, load_kwargs, **k):
        seen["url"], seen["kwargs"] = url, load_kwargs
        return cast_delivery.BridgeOutcome(0.0, 0.0, True, False, tracks=(3,))

    monkeypatch.setattr(remux.cast_delivery, "drive_bridge", drive)
    out = remux.cast_live(
        _cfg(), "T", "http://debrid/x", device="10.0.0.5", audio_index=0, follow=False,
        embedded=(1, "ita"), sub_paths=("/ignored.srt",),
    )  # fmt: skip
    assert out is not None and out.subs_delivered
    assert seen["job"].sub_map == "0:s:1" and seen["job"].sub_lang == "ita"
    assert seen["url"].endswith("/s0master.m3u8") and seen["kwargs"]["text_language"] == "it"
    assert seen["sub_path"] is None and "subtitle_url" not in seen["kwargs"]
    st = remux._read_state() or {}
    assert st["subs"] is True and st["text_language"] == "it"


def test_remux_to_file_extracts_the_embedded_subtitle_in_the_same_pass(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    seen: dict = {}

    def run(cmd, duration, **_kw):
        seen["cmd"] = cmd
        mp4 = cmd[cmd.index("+faststart") + 1]
        Path(mp4).write_bytes(b"x" * 1024)
        Path(cmd[-1]).write_text("WEBVTT\n\n00:01.000 --> 00:02.000\nCiao\n")
        return 0, ""

    monkeypatch.setattr(remux, "_run_ffmpeg", run)
    audio = [Track(id=1, lang="ita", codec="ac3", channels=6)]
    path = remux.remux_to_file("http://x", _cfg(), audio_index=0, audio=audio, sub_index=1)
    assert path is not None
    cmd = seen["cmd"]
    assert cmd[-5:] == ["-map", "0:s:1", "-c:s", "webvtt", remux.embedded_vtt(path)]
    assert Path(remux.embedded_vtt(path)).read_text().startswith("WEBVTT")


def test_live_sub_shift_accumulates_and_never_reloads(monkeypatch, tmp_path):
    """A re-LOAD left the TV IDLE or stuck BUFFERING (field 2026-10-02): the shift only
    writes its file, serve applies it to the segments fetched from then on."""
    loads = _live_state(monkeypatch, tmp_path, pos=1234.0)
    assert remux.live_sub_shift("10.0.0.5", 1.5) == 1.5
    assert remux.live_sub_shift("10.0.0.5", -0.5) == 1.0
    st = remux._read_state() or {}
    assert Path(st["file"], remux.serve.SUB_SHIFT).read_text() == "1.0"
    assert loads == []


def test_live_alignment_reports_the_verdict_and_never_reloads(monkeypatch, tmp_path):
    loads = _live_state(monkeypatch, tmp_path, pos=1234.0)
    st = remux._read_state() or {}
    Path(st["file"], remux.live.ALIGN_FILE).write_text('{"reason": "aligned", "offset": 0.4}')
    assert remux.live_alignment("10.0.0.5") == {"reason": "aligned", "offset": 0.4}
    assert loads == []


def test_live_sub_shift_without_a_live_cast_is_none():
    assert remux.live_sub_shift("10.0.0.5", 1.0) is None


def test_stop_and_replace_never_signal_an_inproc_live_owner(monkeypatch, tmp_path):
    """A followed (TUI) live cast records its own pid: --stop and a new cast must stop the
    receiver, not SIGTERM the interactive process (killpg would take the terminal)."""
    killed, stopped = [], []
    monkeypatch.setattr(remux, "_kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(remux.bridge, "stop", lambda dev: stopped.append(dev))
    d = tmp_path / "cast-x.hls"
    d.mkdir()
    remux._write_state(os.getpid(), str(d), "10.0.0.5", mode="live", inproc=True)
    assert remux.stop() is True
    assert stopped == ["10.0.0.5"] and killed == [] and d.exists()
    remux._write_state(os.getpid(), str(d), "10.0.0.5", mode="live", inproc=True)
    remux._replace_previous(remux._read_state() or {})
    assert killed == [] and remux._read_state() is None


def test_cast_live_adopts_a_matching_prefetch(monkeypatch):
    """Binge: the next episode's producer, started ahead, is adopted by the real start —
    no new serve is spawned — when release and job match; a stale one is reaped."""
    from nstream import live

    seen = _live_wiring(monkeypatch, writes_segments=True)
    assert remux.prefetch_live(
        _cfg(), "http://debrid/a", device="10.0.0.5", audio_index=0, source_key="hash:1"
    )
    prefetched_dir = seen["dir"]
    monkeypatch.setattr(
        remux.serve, "spawn_detached", lambda *a, **k: pytest.fail("must adopt, not spawn")
    )
    out = remux.cast_live(
        _cfg(), "T", "http://debrid/a-new-link", device="10.0.0.5", audio_index=0,
        follow=False, source_key="hash:1",
    )  # fmt: skip
    assert out is not None and out.started
    assert (remux._read_state() or {})["file"] == prefetched_dir
    assert remux._prefetch_state().read() is None
    assert Path(prefetched_dir, live.PLAYLIST).exists()


def test_a_prefetch_for_another_release_is_reaped(monkeypatch):
    seen = _live_wiring(monkeypatch, writes_segments=True)
    remux.prefetch_live(
        _cfg(), "http://debrid/a", device="10.0.0.5", audio_index=0, source_key="hash:1"
    )
    stale = seen["dir"]
    remux.cast_live(
        _cfg(), "T", "http://debrid/b", device="10.0.0.5", audio_index=0,
        follow=False, source_key="hash:2",
    )  # fmt: skip
    assert not Path(stale).exists() and remux._prefetch_state().read() is None


def test_near_end_hook_fires_once(monkeypatch):
    import threading

    seen = _live_wiring(monkeypatch, writes_segments=True)
    fired = []
    done = threading.Event()

    def drive(device, url, *, follow, load_kwargs, on_event=None, **k):
        assert on_event is not None
        for pos in (100.0, 5500.0, 5600.0):  # duration 6000: 90 % = 5400
            on_event({"kind": "playing", "position": pos})
        return cast_delivery.BridgeOutcome(5600.0, 6000.0, True, False)

    monkeypatch.setattr(remux.cast_delivery, "drive_bridge", drive)
    remux.cast_live(
        _cfg(), "T", "http://debrid/x", device="10.0.0.5", audio_index=0, follow=False,
        on_near_end=lambda: fired.append(1) or done.set(),
    )  # fmt: skip
    assert done.wait(2) and fired == [1]
    assert seen
