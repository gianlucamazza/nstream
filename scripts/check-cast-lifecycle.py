"""Real HLS lifetime smoke: lose the observer and exit its process, without a TV LOAD."""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from nstream import util

with tempfile.TemporaryDirectory(prefix="nstream-cast-lifetime-") as work:
    root = Path(work)
    source = root / "source.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "14",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "ultrafast",
            "-g",
            "50",
            "-c:a",
            "ac3",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    env = dict(os.environ)
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        env[key] = work
    parent = """
import sys
from nstream import remux, cast_delivery
from nstream.config import Config
remux.bridge.bridge_available = lambda: True
# Replace only the control transport: the producer, HTTP server and probe are real.
def disconnect(*args, on_started=None, **kwargs):
    on_started()
    return cast_delivery.BridgeOutcome(3, 14, True, True, end_reason="disconnected")
remux.cast_delivery.drive_bridge = disconnect
out = remux.cast_live(Config(torrentio_base="unused", sub_align=False), "Lifetime test",
                     sys.argv[1], device="127.0.0.1", audio_index=0, follow=True)
assert out and out.end_reason == "disconnected"
"""
    pid = None
    try:
        proc = subprocess.run(
            [sys.executable, "-c", parent, str(source)],
            env=env,
            capture_output=True,
            text=True,
            timeout=40,
        )
        state_file = root / "nstream-remux.json"
        if state_file.exists():
            state = json.loads(state_file.read_text())
            pid = state.get("pid")
        assert proc.returncode == 0, "cast observer failed"
        assert pid and util.pid_alive(pid), "server died with observer"
        assert not state.get("inproc"), "server belongs to the observer"
        # Never print the capability URL/token, even on an assertion failure.
        with urllib.request.urlopen(state["url"], timeout=5) as response:
            playlist = response.read().decode()
        segments = [line for line in playlist.splitlines() if line.endswith(".ts")]
        assert segments, "no produced segments"
        segment_url = state["url"].rsplit("/", 1)[0] + "/" + segments[0]
        with urllib.request.urlopen(segment_url, timeout=5) as response:
            assert response.read(), "empty media after observer exit"
        print(
            "PASS: HLS survives control loss and observer exit; playlist and media return HTTP 200"
        )
    finally:
        if pid:
            util.kill_pid(pid, pgroup=True)
