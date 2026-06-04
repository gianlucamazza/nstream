"""Unit tests for the Tier-2 cast remux module.

Process and disk I/O are mocked: tests never spawn ffmpeg/catt or touch the receiver.
The module keeps a small on-disk state file (detached serving PID + temp path); the
autouse fixture points it at a tmp path and clears it so tests don't leak state.
"""

from __future__ import annotations

import json

import pytest

from nstream import remux
from nstream.config import Config
from nstream.tracks import Track, Tracks


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(remux, "_state_path", lambda: tmp_path / "remux.json")
    monkeypatch.setattr(remux, "_cache_dir", lambda: tmp_path)
    yield


def _cfg(**kw) -> Config:
    return Config(torrentio_base="tb", **kw)


# --- needs_remux ----------------------------------------------------------


@pytest.mark.parametrize("codec", ["ac3", "eac3", "dts", "dtshd", "truehd", "AC3", "TrueHD"])
def test_needs_remux_true_for_dolby_dts(codec):
    assert remux.needs_remux(codec) is True


@pytest.mark.parametrize("codec", ["aac", "opus", "flac", "mp3", "", None])
def test_needs_remux_false_for_decodable(codec):
    assert remux.needs_remux(codec) is False


# --- should_remux / prepare_for_cast --------------------------------------


def test_should_remux_off_when_disabled(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_probe_audio", lambda _u: "ac3")
    assert remux.should_remux("http://x", _cfg(cast_remux=False)) is False


def test_should_remux_off_when_no_ffmpeg(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: False)
    assert remux.should_remux("http://x", _cfg(cast_remux=True), hint="ac3") is False


def test_should_remux_uses_ffprobe_truth_over_hint(monkeypatch):
    # Release name says AAC, but the real track is E-AC-3 → remux.
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_probe_audio", lambda _u: "eac3")
    assert remux.should_remux("http://x", _cfg(cast_remux=True), hint="aac") is True


def test_should_remux_falls_back_to_hint_when_probe_empty(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_probe_audio", lambda _u: "")
    assert remux.should_remux("http://x", _cfg(cast_remux=True), hint="dts") is True
    assert remux.should_remux("http://x", _cfg(cast_remux=True), hint="aac") is False


def test_probe_audio_reads_headline_track(monkeypatch):
    from nstream import tracks

    monkeypatch.setattr(
        tracks,
        "probe_tracks",
        lambda _u: Tracks(audio=[Track(id=1, codec="EAC3"), Track(id=2, codec="aac")]),
    )
    assert remux._probe_audio("http://x") == "eac3"


def test_prepare_for_cast_none_when_not_needed(monkeypatch):
    monkeypatch.setattr(remux, "should_remux", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(remux, "remux_to_file", lambda *a, **k: called.append(1) or "/tmp/x.mp4")
    assert remux.prepare_for_cast("http://x", _cfg()) is None
    assert called == []  # no remux attempted


def test_prepare_for_cast_remuxes_when_needed(monkeypatch):
    monkeypatch.setattr(remux, "should_remux", lambda *a, **k: True)
    monkeypatch.setattr(remux, "remux_to_file", lambda *a, **k: "/tmp/cast-x.mp4")
    assert remux.prepare_for_cast("http://x", _cfg()) == "/tmp/cast-x.mp4"


# --- remux_to_file --------------------------------------------------------


class _P:
    def __init__(self, rc=0, stderr=""):
        self.returncode = rc
        self.stderr = stderr


def test_remux_to_file_success(monkeypatch, tmp_path):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        # Simulate ffmpeg writing a non-empty output at the path (last arg).
        with open(cmd[-1], "wb") as f:
            f.write(b"x" * 1024)
        return _P(0)

    monkeypatch.setattr(remux.subprocess, "run", fake_run)
    path = remux.remux_to_file("http://x?token=secret", _cfg(cast_audio_codec="aac"))
    assert path and path.endswith(".mp4")
    assert "-c:v" in seen["cmd"] and "copy" in seen["cmd"]
    assert "aac" in seen["cmd"]
    # The token-bearing url is an ffmpeg arg (passed to the process), never returned.
    assert "secret" not in path


def test_remux_to_file_failure_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)

    def fake_run(cmd, **kw):
        return _P(1, "boom")  # ffmpeg failed; no output written

    monkeypatch.setattr(remux.subprocess, "run", fake_run)
    assert remux.remux_to_file("http://x", _cfg()) is None
    # No leftover temp file in the cache dir.
    assert list(tmp_path.glob("cast-*.mp4")) == []


def test_remux_to_file_none_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: False)
    assert remux.remux_to_file("http://x", _cfg()) is None


# --- stop / state ---------------------------------------------------------


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
