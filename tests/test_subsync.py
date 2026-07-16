"""Unit tests for the audio-anchored subtitle correction (alass, ADR 0019)."""

from __future__ import annotations

from nstream import subsync


def test_available_requires_both_tools(monkeypatch):
    monkeypatch.setattr(subsync.shutil, "which", lambda b: "/usr/bin/x" if b == "ffmpeg" else None)
    assert subsync.available() is False
    monkeypatch.setattr(subsync.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert subsync.available() is True


def test_parse_offset_seconds_and_ms():
    assert subsync._parse_offset("shifted block of 1714 subtitles by -13.9s") == -13.9
    assert subsync._parse_offset("shifted block of 3 subtitles by 750ms") == 0.75
    # several blocks → the largest |shift| is the one worth reporting
    assert subsync._parse_offset("by 100ms\nby -14s\nby 2s") == -14.0
    assert subsync._parse_offset("no numbers here") is None


class _Proc:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_sync_to_audio_happy_path(monkeypatch, tmp_path):
    """ffmpeg extracts the bounded reference, alass --no-split corrects in place."""
    srt = tmp_path / "sub.srt"
    srt.write_text("1\n00:00:20,000 --> 00:00:21,000\nciao\n")
    cmds: list[list[str]] = []

    def fake_run(cmd, *, timeout=None, **kw):
        cmds.append(cmd)
        if cmd[0] == "ffmpeg":
            (tmp_path / "subsync-ref.wav").write_bytes(b"RIFF")
            return _Proc()
        (tmp_path / "subsync-out.srt").write_text("1\n00:00:06,100 --> 00:00:07,100\nciao\n")
        return _Proc(out="shifted block of 1 subtitles by -13.9s")

    monkeypatch.setattr(subsync.util, "run_cmd", fake_run)
    ran, offset = subsync.sync_to_audio(str(srt), "http://cdn/v.mkv", str(tmp_path), window_s=600)
    assert ran is True and offset == -13.9
    assert srt.read_text().startswith("1\n00:00:06,100")  # corrected IN PLACE
    assert cmds[0][0] == "ffmpeg" and "-t" in cmds[0] and "600" in cmds[0]
    assert cmds[1][:2] == ["alass", "--no-split"]


def test_sync_to_audio_extraction_failure_is_best_effort(monkeypatch, tmp_path):
    srt = tmp_path / "sub.srt"
    srt.write_text("x")
    monkeypatch.setattr(subsync.util, "run_cmd", lambda *a, **k: None)  # ffmpeg missing/timeout
    assert subsync.sync_to_audio(str(srt), "http://u", str(tmp_path)) == (False, None)
    assert srt.read_text() == "x"  # untouched


def test_sync_to_audio_alass_failure_keeps_original(monkeypatch, tmp_path):
    srt = tmp_path / "sub.srt"
    srt.write_text("original")

    def fake_run(cmd, *, timeout=None, **kw):
        if cmd[0] == "ffmpeg":
            (tmp_path / "subsync-ref.wav").write_bytes(b"RIFF")
            return _Proc()
        return _Proc(rc=1)

    monkeypatch.setattr(subsync.util, "run_cmd", fake_run)
    assert subsync.sync_to_audio(str(srt), "http://u", str(tmp_path)) == (False, None)
    assert srt.read_text() == "original"
