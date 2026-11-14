"""Executable ffprobe/mpv protocol double for subprocess acceptance tests."""

import json
import os
import socket
import sys
import time
from pathlib import Path

if Path(sys.argv[0]).name == "catt":
    # What the receiver "plays": the last cast url, reported back as content_id like a TV.
    loaded = Path(sys.argv[0]).with_name("catt-loaded")
    if "scan" in sys.argv:
        print("127.0.0.1 - Fixture TV - Simulator")
    elif "info" in sys.argv:
        print(
            json.dumps(
                {
                    "duration": 600,
                    "current_time": 42,
                    "player_state": "PLAYING",
                    "volume_level": 0.5,
                    "volume_muted": False,
                    "title": "Fixture",
                    "content_id": loaded.read_text() if loaded.exists() else "",
                }
            )
        )
    elif "cast" in sys.argv and os.environ.get("NSTREAM_TEST_CAST_FAIL") == "1":
        print("https://example.test/SECRET", file=sys.stderr)
        sys.exit(1)
    elif "cast" in sys.argv:
        urls = [a for a in sys.argv if a.startswith("http")]
        if urls:
            loaded.write_text(urls[0])
    sys.exit(0)

if Path(sys.argv[0]).name == "ffprobe":
    print(
        json.dumps(
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
                    {"codec_type": "audio", "codec_name": "aac", "tags": {"language": "ita"}},
                ],
                "format": {"duration": "600", "format_name": "mov,mp4"},
            }
        )
    )
    sys.exit(0)

if os.environ.get("NSTREAM_TEST_PLAYER_FAIL") == "1":
    print("https://example.test/SECRET", file=sys.stderr)
    sys.exit(2)

path = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--input-ipc-server="))
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
    server.settimeout(5)
    server.bind(path)
    server.listen(1)
    connection, _ = server.accept()
    with connection:
        connection.recv(4096)
        for name, value in (("duration", 600), ("time-pos", 42)):
            message = {"event": "property-change", "name": name, "data": value}
            connection.sendall((json.dumps(message) + "\n").encode())
        time.sleep(0.1)
