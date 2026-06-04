"""Unit tests for the Tier-2 cast remux module (the executor).

Process and disk I/O are mocked: tests never spawn ffmpeg/catt or touch the receiver.
The cast-language *decision* (which track, direct vs remux) lives in `stream_select`
(`vet_cast_audio`) and is tested there; here we test the file-production executor.
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


def _meta(audio=(), n_video=1, duration=0.0):
    """Build a `_probe_meta` return tuple (Tracks, n_video, duration)."""
    return Tracks(audio=list(audio)), n_video, duration


# --- needs_remux / _decodable ---------------------------------------------


@pytest.mark.parametrize("codec", ["ac3", "eac3", "dts", "dtshd", "truehd", "AC3", "TrueHD"])
def test_needs_remux_true_for_dolby_dts(codec):
    assert remux.needs_remux(codec) is True


@pytest.mark.parametrize("codec", ["aac", "opus", "flac", "mp3", "", None])
def test_needs_remux_false_for_decodable(codec):
    assert remux.needs_remux(codec) is False


@pytest.mark.parametrize("codec,ok", [("aac", True), ("opus", True), ("ac3", False), ("", False)])
def test_decodable(codec, ok):
    assert remux._decodable(codec) is ok


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

    def fake_remux(url, cfg, *, audio_index, audio, n_video, duration, size_gb):
        seen.update(audio_index=audio_index, n_video=n_video, duration=duration, size_gb=size_gb)
        return "/tmp/cast-x.mp4"

    monkeypatch.setattr(remux, "remux_to_file", fake_remux)
    out = remux.remux_for_cast("http://x", _cfg(), audio_index=3, size_gb=12.0)
    assert out == "/tmp/cast-x.mp4"
    assert seen == {"audio_index": 3, "n_video": 2, "duration": 99.0, "size_gb": 12.0}


# --- _audio_bitrate -------------------------------------------------------


@pytest.mark.parametrize(
    "channels,expected", [(None, "192k"), (2, "192k"), (6, "448k"), (8, "640k")]
)
def test_audio_bitrate_scales_with_channels(channels, expected):
    assert remux._audio_bitrate(channels) == expected


# --- remux_to_file --------------------------------------------------------


def _fake_ffmpeg_ok(seen):
    def run(cmd, duration):
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
    audio = [Track(id=1, lang="ita", codec="eac3", channels=6, index=1)]
    path = remux.remux_to_file("http://x?token=secret", _cfg(), audio_index=1, audio=audio)
    cmd = seen["cmd"]
    assert path and path.endswith(".mp4")
    assert "-c:v" in cmd and "copy" in cmd  # video always copied
    assert "-c:a" in cmd and "aac" in cmd and "448k" in cmd  # 5.1 EAC3 → AAC 448k
    assert "0:1?" in cmd  # absolute stream index of the chosen track
    assert "secret" not in path  # token-bearing url never returned


def test_remux_to_file_copies_decodable_track(monkeypatch, tmp_path):
    # Selecting a non-default AAC track only drops the others → stream-copy, no re-encode.
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    seen = {}
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok(seen))
    audio = [
        Track(id=1, lang="eng", codec="aac", index=1),
        Track(id=2, lang="ita", codec="aac", index=2),
    ]
    remux.remux_to_file("http://x", _cfg(), audio_index=2, audio=audio)
    cmd = seen["cmd"]
    assert "0:2?" in cmd  # absolute index of the requested (Italian) track
    assert cmd[cmd.index("-c:a") + 1] == "copy"  # already decodable → copy
    assert "-b:a" not in cmd


def test_remux_to_file_failure_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda cmd, duration: (1, "boom"))
    assert remux.remux_to_file("http://x", _cfg()) is None
    assert list(tmp_path.glob("cast-*.mp4")) == []  # no leftover temp file


def test_remux_to_file_none_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: False)
    assert remux.remux_to_file("http://x", _cfg()) is None


def test_remux_to_file_aborts_on_insufficient_disk(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 5.0)
    ran = []
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda *a: ran.append(1) or (0, ""))
    assert remux.remux_to_file("http://x", _cfg(), size_gb=40.0) is None
    assert ran == []  # ffmpeg never launched


def test_remux_to_file_size_cap_confirm_declined(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 500.0)
    monkeypatch.setattr(remux, "_confirm", lambda _m: False)
    ran = []
    monkeypatch.setattr(remux, "_run_ffmpeg", lambda *a: ran.append(1) or (0, ""))
    assert remux.remux_to_file("http://x", _cfg(cast_remux_max_size_gb=10), size_gb=30.0) is None
    assert ran == []


def test_remux_to_file_size_cap_confirm_accepted(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    monkeypatch.setattr(remux, "_free_gb", lambda _p: 500.0)
    monkeypatch.setattr(remux, "_confirm", lambda _m: True)
    seen = {}
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok(seen))
    out = remux.remux_to_file("http://x", _cfg(cast_remux_max_size_gb=10), size_gb=30.0)
    assert out and "cmd" in seen


def test_remux_to_file_warns_on_dv7_dual_layer(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok({}))
    warns: list[str] = []
    monkeypatch.setattr(remux._log, "warning", lambda msg, *a: warns.append(str(msg)))
    remux.remux_to_file("http://x", _cfg(), n_video=2)
    assert any("dual-layer" in w for w in warns)


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
