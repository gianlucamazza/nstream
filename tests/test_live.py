"""Live HLS-TS producer (ADR 0039): argv, playlist timeline, disk policy, and its route."""

from __future__ import annotations

import signal
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

from nstream import live, serve


def _playlist(d: Path, n: int, seg: float = 6.0) -> None:
    lines = ["#EXTM3U", "#EXT-X-PLAYLIST-TYPE:EVENT"]
    for i in range(n):
        lines += [f"#EXTINF:{seg:.6f},", f"index{i}.ts"]
        (d / f"index{i}.ts").write_bytes(b"\x47" * 188)
    (d / live.PLAYLIST).write_text("\n".join(lines) + "\n")


class _Proc:
    def __init__(self):
        self.signals: list[int] = []
        self.rc = None

    def poll(self):
        return self.rc

    def send_signal(self, sig):
        self.signals.append(sig)


def test_producer_cmd_is_token_free_stereo_hls_ts(tmp_path):
    cmd = live.producer_cmd("http://127.0.0.1:1/route", str(tmp_path), live.Job("http://x"))
    joined = " ".join(cmd)
    assert "http://x" not in joined  # the job url never reaches the argv
    assert cmd[cmd.index("-i") + 1] == "http://127.0.0.1:1/route"
    assert cmd[cmd.index("-c:v") : cmd.index("-c:v") + 2] == ["-c:v", "copy"]
    assert "-ac 2" in joined and "aac" in joined  # AAC 5.1 stalls this receiver in HLS
    assert "-hls_segment_type mpegts" in joined and "-hls_playlist_type event" in joined
    assert "temp_file" in joined  # never serve a half-written segment


def test_job_roundtrip():
    job = live.Job("http://x", "0:a:2", ("-c:a", "copy"), head_s=1036.0)
    assert live.Job.from_dict(job.to_dict()) == job


def test_timeline_and_produced(tmp_path):
    _playlist(tmp_path, 3, seg=6.0)
    assert live.timeline(str(tmp_path)) == [(0, 0.0, 6.0), (1, 6.0, 6.0), (2, 12.0, 6.0)]
    assert live.produced_s(str(tmp_path)) == 18.0
    assert live.produced_s(str(tmp_path / "missing")) == 0.0


def test_on_request_prunes_behind_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "KEEP_BEHIND_S", 12.0)
    _playlist(tmp_path, 10)
    p = live.Producer(str(tmp_path), cast(Any, _Proc()))
    p.on_request("index5.ts")  # play head at 30 s → keep from 18 s
    left = sorted(int(f.stem[5:]) for f in tmp_path.glob("index*.ts"))
    assert left == [3, 4, 5, 6, 7, 8, 9]
    p.on_request("index2.ts")  # a backward request never moves the head back
    assert p.newest_s == 30.0


def test_tick_pauses_far_ahead_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "AHEAD_MAX_S", 30.0)
    monkeypatch.setattr(live, "AHEAD_RESUME_S", 15.0)
    _playlist(tmp_path, 10)  # 60 s produced
    proc = _Proc()
    p = live.Producer(str(tmp_path), cast(Any, proc))
    p.tick()
    assert p.paused and proc.signals == [signal.SIGSTOP]
    p.on_request("index8.ts")  # head at 48 s → 12 s ahead
    p.tick()
    assert not p.paused and proc.signals[-1] == signal.SIGCONT


def test_resume_head_lets_the_producer_reach_it(tmp_path, monkeypatch):
    """A resume at 1036 s must not pause the producer at AHEAD_MAX before reaching it."""
    monkeypatch.setattr(live, "AHEAD_MAX_S", 30.0)
    _playlist(tmp_path, 10)
    proc = _Proc()
    p = live.Producer(str(tmp_path), cast(Any, proc), newest_s=1036.0)
    p.tick()
    assert proc.signals == []


@pytest.fixture
def hls_server(tmp_path):
    _playlist(tmp_path, 4)
    (tmp_path / "ffmpeg.log").write_text("diag")
    server, port, _t = serve.serve_file(None, "127.0.0.1", hls_dir=str(tmp_path))
    seen: list[int] = []

    class _P:
        def on_request(self, i):
            seen.append(i)

    server.producer = cast(Any, _P())
    base = f"http://127.0.0.1:{port}{serve.hls_url_path(server.token)}"
    yield base, seen
    server.shutdown()


def test_hls_route_serves_playlist_and_segments(hls_server):
    base, seen = hls_server
    with urllib.request.urlopen(base + live.PLAYLIST, timeout=5) as r:
        assert r.headers["Content-Type"] == "application/vnd.apple.mpegurl"
        assert r.headers["Cache-Control"] == "no-cache"
        assert b"index3.ts" in r.read()
    with urllib.request.urlopen(base + "index2.ts", timeout=5) as r:
        assert r.headers["Content-Type"] == "video/mp2t"
        r.read()
    assert (
        seen[-1] == "index2.ts"
    )  # the segment request reached the producer (it ignores the playlist)


@pytest.mark.parametrize("name", ["ffmpeg.log", "../index0.ts", "index0.ts.tmp", "x.m3u8"])
def test_hls_route_whitelists_names(hls_server, name):
    base, _ = hls_server
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + name, timeout=5)
    assert e.value.code == 404


def test_spawn_detached_passes_the_job_on_stdin(monkeypatch, tmp_path):
    seen = {}

    class _Pipe:
        def __init__(self):
            self.buf = ""

        def write(self, s):
            self.buf += s

        def close(self):
            seen["stdin"] = self.buf

    class _Popen:
        pid = 4242

        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            self.stdin = _Pipe()
            self.stdout = iter_lines(["PORT=45001\n", "TOKEN=tok\n"])

    class iter_lines:  # noqa: N801 — a readline-able stand-in for the child's stdout
        def __init__(self, lines):
            self.lines = lines

        def readline(self):
            return self.lines.pop(0)

    monkeypatch.setattr(serve.subprocess, "Popen", _Popen)
    out = serve.spawn_detached(
        "10.0.0.2", hls_dir=str(tmp_path), job=live.Job("http://debrid/realdebrid=SECRET/f")
    )
    assert out == (4242, 45001, "tok")
    assert "SECRET" not in " ".join(seen["cmd"]) and "--hls" in seen["cmd"]
    assert "SECRET" in seen["stdin"]


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg missing")
def test_real_producer_turns_ac3_mkv_into_aac_hls(tmp_path):
    """End to end with the real ffmpeg: an AC-3 MKV (the case the DMR plays mute) becomes
    MPEG-TS segments with AAC stereo audio and a growing EVENT playlist."""
    import subprocess
    import time

    src = tmp_path / "src.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "14",
         "-ac", "6", "-c:v", "libx264", "-g", "50", "-c:a", "ac3", str(src)],
        check=True,
    )  # fmt: skip
    out = tmp_path / "live"
    out.mkdir()
    p = live.Producer.start(live.Job(str(src)), str(out))
    assert p is not None
    deadline = time.monotonic() + 30
    while p.proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    assert p.proc.returncode == 0, p.failure_reason()
    assert live.produced_s(str(out)) == pytest.approx(14.0, abs=1.0)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,channels", "-of", "csv=p=0", str(out / "index0.ts")],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert probe.stdout.split()[0] == "aac,2"
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in (out / live.PLAYLIST).read_text()
    p.stop()
    assert not out.exists()


def test_producer_cmd_fast_resume_seeks_input_and_keeps_timestamps(tmp_path):
    cmd = live.producer_cmd("http://127.0.0.1:1/r", str(tmp_path), live.Job("u", ss_s=4520.0))
    assert cmd[cmd.index("-ss") + 1] == "4520.000" and cmd.index("-ss") < cmd.index("-i")
    assert "-noaccurate_seek" in cmd and "-copyts" in cmd
    plain = live.producer_cmd("http://127.0.0.1:1/r", str(tmp_path), live.Job("u"))
    assert "-ss" not in plain and "-copyts" not in plain


def test_restart_moves_to_a_new_generation(tmp_path, monkeypatch):
    """Seek anywhere: generation N+1 starts at the requested film time; the old
    generation's playlist and segments are removed; stale requests are ignored."""
    _playlist(tmp_path, 3)
    spawned = []

    def fake_spawn(job, out_dir, gen):
        spawned.append((job.ss_s, gen))
        return _Proc()

    monkeypatch.setattr(live, "_spawn", fake_spawn)
    monkeypatch.setattr(live, "_terminate", lambda proc: None)
    p = live.Producer(str(tmp_path), cast(Any, _Proc()), job=live.Job("u"), newest_s=100.0)
    assert p.restart(4000.0, 1) is True
    assert spawned == [(4000.0, 1)] and p.gen == 1 and p.newest_s == 0.0
    assert not (tmp_path / live.PLAYLIST).exists() and not list(tmp_path.glob("index*.ts"))
    assert p.restart(10.0, 1) is False  # a stale/duplicate request never goes back
    p.on_request("index2.ts")  # the old generation: ignored
    assert p.newest_s == 0.0


def test_generation_names():
    assert live.playlist_name(0) == "index.m3u8" and live.playlist_name(2) == "g2.m3u8"
    assert live.segment_id("g2_14.ts") == (2, 14) and live.segment_id("index3.ts") == (0, 3)
    assert live.segment_id("g2_14.ts.tmp") is None and not live.is_playlist_name("x.m3u8")


def test_serve_restart_request_reaches_the_producer(tmp_path):
    seen = []

    class _P:
        def request_restart(self, ss, gen):
            seen.append((ss, gen))

    (tmp_path / serve.RESTART_REQUEST).write_text('{"ss": 1200.5, "gen": 2}')
    serve._restart_from_request(cast(Any, _P()), str(tmp_path))
    (tmp_path / serve.RESTART_REQUEST).write_text("garbage")
    serve._restart_from_request(cast(Any, _P()), str(tmp_path))  # logged, not raised
    assert seen == [(1200.5, 2)]


def test_restart_runs_on_the_long_lived_pacing_thread(tmp_path, monkeypatch):
    """PR_SET_PDEATHSIG follows the spawning thread: the restart must run on the pacing
    thread (alive as long as the producer), never on the short signal-handler thread."""
    import threading
    import time

    _playlist(tmp_path, 3)
    threads = []
    monkeypatch.setattr(
        live, "_spawn", lambda job, out, gen: threads.append(threading.current_thread()) or _Proc()
    )
    monkeypatch.setattr(live, "_terminate", lambda proc: None)
    p = live.Producer(str(tmp_path), cast(Any, _Proc()), job=live.Job("u"))
    pacing = p.run_pacing(interval=0.05)
    p.request_restart(1800.0, 1)
    deadline = time.monotonic() + 2
    while not threads and time.monotonic() < deadline:
        time.sleep(0.02)
    p._stopped = True
    assert threads == [pacing] and p.gen == 1
