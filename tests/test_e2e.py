"""End-to-end smoke tests: run `nstream` as a real subprocess and assert the `--json`
harness contract holds against an actual invocation, not a mocked in-process call.

The rest of the suite is unit-level with mocks (the cast decision tree, selection, etc.).
This file is the one place that spawns the CLI for real, guarding the invariants the
`nstream` skill relies on: **exactly one JSON object on stdout, nothing else** (no banner,
no traceback, no leaked stream/debrid URL or token), and a non-zero exit on a usage error.

Only deterministic, network-free paths are exercised here (argument-validation errors),
so the tests stay hermetic — no addon HTTP, no external tool, no Chromecast."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nstream.config import config_path, state_path


@pytest.fixture
def playback_environment(tmp_path, monkeypatch):
    class Addon(BaseHTTPRequestHandler):
        server: ThreadingHTTPServer

        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self):
            title = {"id": "tt1234567", "type": "movie", "name": "Fixture", "runtime": "10 min"}
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            if self.path.endswith("manifest.json"):
                body = {
                    "id": "fixture",
                    "name": "Fixture",
                    "version": "1.0.0",
                    "types": ["movie"],
                    "resources": ["stream"],
                    "catalogs": [],
                }
            elif "/catalog/" in self.path:
                body = {"metas": [title]}
            elif "/meta/" in self.path:
                body = {"meta": title}
            elif "/stream/" in self.path:
                body = {
                    "streams": [
                        {
                            "name": "Fixture 1080p",
                            "title": "Fixture.1080p.H264.ITA",
                            "url": base + "/SECRET.mp4",
                        }
                    ]
                }
            else:
                body = {}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Addon)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    config_path().write_text(
        json.dumps(
            {
                "cinemeta": base,
                "opensubtitles": base,
                "torrentio_enabled": False,
                "addons": [base + "/addon/manifest.json"],
                "prefer_cast": False,
                "default_quality": 0,
                "hwdec": "",
                "sub_align": False,
            }
        )
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    script = (Path(__file__).parent / "data" / "backend.py").read_text()
    for name in ("mpv", "ffprobe", "catt"):
        executable = binaries / name
        executable.write_text(f"#!{sys.executable}\n" + script)
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries))
    # Point the native helpers at nothing. Unset, `bridge._binary` falls back to the
    # developer's real castbridge path: on a dev machine the subprocess then spawned a
    # REAL daemon per cast test, orphaned past the suite (216 found on 2026-10-02).
    for variable in ("CASTBRIDGE_BIN", "CAST_MIRROR_BIN"):
        monkeypatch.setenv(variable, str(tmp_path / f"no-{variable.lower()}"))
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("failure", [False, True])
def test_cli_search_to_player_protocol_and_history(playback_environment, monkeypatch, failure):
    if failure:
        monkeypatch.setenv("NSTREAM_TEST_PLAYER_FAIL", "1")
    proc = _run(["--json", "--local", "--movies", "Fixture"])
    report = json.loads(proc.stdout)
    assert "SECRET" not in proc.stdout + proc.stderr
    if failure:
        assert proc.returncode == 1 and report["error"] == "player_failed"
        assert not state_path().exists()
    else:
        assert proc.returncode == 0 and report["ok"] and report["action"] == "play"
        assert json.loads(state_path().read_text())["tt1234567"]["position"] == 42


def test_argparse_error_is_json_without_config():
    config_path().unlink()
    proc = _run(["--json", "--season", "invalid"])
    assert proc.returncode == 2 and json.loads(proc.stdout)["error"] == "usage"


def test_doctor_cli_does_not_create_state():
    config_path().unlink()
    proc = _run(["--json", "--doctor"])
    assert json.loads(proc.stdout)["action"] == "doctor"
    assert not state_path().parent.exists()


@pytest.mark.parametrize("failure", [False, True])
def test_cli_cast_protocol_success_and_failure(playback_environment, monkeypatch, failure):
    if failure:
        monkeypatch.setenv("NSTREAM_TEST_CAST_FAIL", "1")
    proc = _run(["--json", "--cast", "--device", "Fixture TV", "--movies", "Fixture"])
    report = json.loads(proc.stdout)
    assert "SECRET" not in proc.stdout + proc.stderr
    assert report["ok"] is not failure
    if failure:
        assert proc.returncode == 1 and report["error"] == "cast_failed"
        assert not state_path().exists()
    else:
        assert proc.returncode == 0 and report["action"] == "cast"
        stop = _run(["--json", "--stop", "--device", "Fixture TV"])
        assert stop.returncode == 0 and json.loads(stop.stdout)["ok"]
        assert json.loads(state_path().read_text())["tt1234567"]["position"] == 42


def test_stalled_addon_does_not_hold_process_exit():
    code = (
        "from nstream import api; import time; "
        "api._GATHER_BUDGET = 0.02; "
        "assert api._gather([lambda: time.sleep(60) or []]) == []"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=3)
    assert proc.returncode == 0


# Argument-validation paths: each prints exactly one JSON object and exits non-zero, with no
# network I/O — the hermetic slice of the `--json` surface.
_USAGE_CASES = [
    (["--json", "--mirror", "--no-mirror", "x"], "incompatibili"),
    (["--json", "--quality", "bogus", "x"], "non valida"),
]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nstream", *args],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip


@pytest.mark.parametrize("args,needle", _USAGE_CASES)
def test_json_usage_error_is_single_object(args, needle):
    proc = _run(args)
    # Exactly one JSON object on stdout — json.loads over the whole stripped stdout must parse
    # (a trailing banner/second object would make this raise), and it is a usage error.
    out = proc.stdout.strip()
    obj = json.loads(out)
    assert isinstance(obj, dict)
    assert obj["ok"] is False and obj["error"] == "usage"
    assert needle in obj["message"]
    assert proc.returncode != 0


@pytest.mark.parametrize("args,_needle", _USAGE_CASES)
def test_json_stdout_never_leaks_token_or_traceback(args, _needle):
    proc = _run(args)
    # The JSON-purity invariant: no traceback, no Torrentio/debrid URL or token on stdout.
    assert "Traceback" not in proc.stdout
    for marker in ("torrentio", "realdebrid", "/resolve/", "http://", "https://"):
        assert marker not in proc.stdout.lower()
