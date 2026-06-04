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


def _meta(audio=(), n_video=1, duration=0.0):
    """Build a `_probe_meta` return tuple (Tracks, n_video, duration)."""
    return Tracks(audio=list(audio)), n_video, duration


# --- needs_remux ----------------------------------------------------------


@pytest.mark.parametrize("codec", ["ac3", "eac3", "dts", "dtshd", "truehd", "AC3", "TrueHD"])
def test_needs_remux_true_for_dolby_dts(codec):
    assert remux.needs_remux(codec) is True


@pytest.mark.parametrize("codec", ["aac", "opus", "flac", "mp3", "", None])
def test_needs_remux_false_for_decodable(codec):
    assert remux.needs_remux(codec) is False


# --- should_remux ---------------------------------------------------------


def test_should_remux_off_when_disabled(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_probe_meta", lambda _u: _meta([Track(id=1, codec="ac3")]))
    assert remux.should_remux("http://x", _cfg(cast_remux=False)) is False


def test_should_remux_off_when_no_ffmpeg(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: False)
    assert remux.should_remux("http://x", _cfg(cast_remux=True), hint="ac3") is False


def test_should_remux_skips_probe_on_decodable_hint(monkeypatch):
    # A clearly-decodable name (AAC) is trusted: no probe, no remux (instant Tier-1).
    monkeypatch.setattr(remux, "available", lambda: True)
    probed = []
    monkeypatch.setattr(remux, "_probe_meta", lambda _u: probed.append(1) or _meta())
    assert remux.should_remux("http://x", _cfg(cast_remux=True), hint="aac") is False
    assert probed == []  # probe skipped


def test_should_remux_probes_when_hint_undecodable(monkeypatch):
    # Name says AC-3 → still probe to confirm before a costly remux; probe says E-AC-3.
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_probe_meta", lambda _u: _meta([Track(id=1, codec="eac3")]))
    assert remux.should_remux("http://x", _cfg(cast_remux=True), hint="ac3") is True


def test_should_remux_falls_back_to_hint_when_probe_empty(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_probe_meta", lambda _u: _meta([]))
    assert remux.should_remux("http://x", _cfg(cast_remux=True), hint="dts") is True


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


# --- prepare_for_cast -----------------------------------------------------


def test_prepare_for_cast_none_when_disabled(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    assert remux.prepare_for_cast("http://x", _cfg(cast_remux=False)) is None


def test_prepare_for_cast_none_on_decodable_hint(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    called = []
    monkeypatch.setattr(remux, "remux_to_file", lambda *a, **k: called.append(1) or "/tmp/x.mp4")
    assert remux.prepare_for_cast("http://x", _cfg(), hint="aac") is None
    assert called == []  # no probe, no remux


def test_prepare_for_cast_none_when_decodable_probe(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_probe_meta", lambda _u: _meta([Track(id=1, codec="aac")]))
    called = []
    monkeypatch.setattr(remux, "remux_to_file", lambda *a, **k: called.append(1) or "/tmp/x.mp4")
    assert remux.prepare_for_cast("http://x", _cfg()) is None
    assert called == []


def test_prepare_for_cast_remuxes_and_threads_probe(monkeypatch):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(
        remux,
        "_probe_meta",
        lambda _u: _meta([Track(id=1, codec="eac3", channels=6)], n_video=2, duration=99.0),
    )
    seen = {}

    def fake_remux(url, cfg, *, audio, n_video, duration, size_gb):
        seen.update(audio=audio, n_video=n_video, duration=duration, size_gb=size_gb)
        return "/tmp/cast-x.mp4"

    monkeypatch.setattr(remux, "remux_to_file", fake_remux)
    out = remux.prepare_for_cast("http://x", _cfg(), hint="eac3", size_gb=12.0)
    assert out == "/tmp/cast-x.mp4"
    assert seen["n_video"] == 2 and seen["duration"] == 99.0 and seen["size_gb"] == 12.0
    assert seen["audio"][0].codec == "eac3"


# --- audio track selection + bitrate --------------------------------------


def test_select_audio_index_prefers_primary_language():
    audio = [Track(id=1, lang="ita"), Track(id=2, lang="eng")]
    assert remux._select_audio_index(audio, _cfg(primary_lang="eng")) == 1
    assert remux._select_audio_index(audio, _cfg(primary_lang="ita")) == 0


def test_select_audio_index_falls_back_to_zero():
    audio = [Track(id=1, lang="jpn"), Track(id=2, lang="kor")]
    assert remux._select_audio_index(audio, _cfg(primary_lang="eng", audio_langs=["eng"])) == 0


def test_select_audio_index_single_track_is_zero():
    assert remux._select_audio_index([Track(id=1, lang="ita")], _cfg(primary_lang="eng")) == 0


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


def test_remux_to_file_success(monkeypatch, tmp_path):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    seen = {}
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok(seen))
    path = remux.remux_to_file("http://x?token=secret", _cfg(cast_audio_codec="aac"))
    assert path and path.endswith(".mp4")
    cmd = seen["cmd"]
    assert "-c:v" in cmd and "copy" in cmd and "aac" in cmd
    assert "0:a:0?" in cmd  # no track list → first-track fallback
    assert "secret" not in path  # token-bearing url never returned


def test_remux_to_file_picks_language_track_and_bitrate(monkeypatch, tmp_path):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    seen = {}
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok(seen))
    audio = [Track(id=1, lang="ita", channels=2), Track(id=2, lang="eng", channels=6)]
    remux.remux_to_file("http://x", _cfg(primary_lang="eng"), audio=audio)
    cmd = seen["cmd"]
    assert "0:a:1" in cmd  # English track selected
    assert "448k" in cmd  # 6-channel bitrate


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


def test_remux_to_file_warns_on_dv7_dual_layer(monkeypatch, capsys):
    monkeypatch.setattr(remux, "available", lambda: True)
    monkeypatch.setattr(remux, "_gc_stale", lambda: None)
    seen = {}
    monkeypatch.setattr(remux, "_run_ffmpeg", _fake_ffmpeg_ok(seen))
    remux.remux_to_file("http://x", _cfg(), n_video=2)
    assert "dual-layer" in capsys.readouterr().err


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


class _P:
    def __init__(self, rc=0, stderr=""):
        self.returncode = rc
        self.stderr = stderr
