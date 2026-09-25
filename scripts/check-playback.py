"""Optional real ffmpeg/mpv decode and IPC smoke; no display, speakers, or network."""

import json
import os
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

from nstream.config import Config
from nstream.player import play

with tempfile.TemporaryDirectory(prefix="nstream-decode-") as work:
    root = Path(work)
    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        os.environ[key] = work
    media = root / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "2",
            "-c:v",
            "mpeg4",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(media),
        ],
        check=True,
        timeout=20,
    )
    cfg = Config(
        hwdec="",
        mpv_args=[
            "--no-config",
            "--vo=null",
            "--ao=null",
            "--keep-open=no",
            "--idle=no",
            "--terminal=no",
        ],
    )
    outcome = play(cfg, "Generated fixture", str(media))
    assert outcome.started and outcome.duration > 0 and outcome.reason == "ended"
    print(
        json.dumps(
            {
                "decode_and_ipc": "passed",
                "outcome": asdict(outcome),
                "audiovisual_output_verified": False,
            }
        )
    )
