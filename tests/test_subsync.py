"""Unit tests for the audio-anchored subtitle offset measurement (alass, ADR 0019)."""

from __future__ import annotations

from nstream import subsync


def _cue(i: int, t: int) -> str:
    return f"{i}\n00:{t // 60:02d}:{t % 60:02d},000 --> 00:{t // 60:02d}:{t % 60 + 2:02d},000\nbattuta {i}"


# a cue every 12 s over ~30 min: every 300 s window holds ~25 cues (> _MIN_CUES)
_SRT = "\n\n".join(_cue(i, 12 * i) for i in range(1, 150))


def test_available_requires_both_tools(monkeypatch):
    monkeypatch.setattr(subsync.shutil, "which", lambda b: "/usr/bin/x" if b == "ffmpeg" else None)
    assert subsync.available() is False
    monkeypatch.setattr(subsync.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert subsync.available() is True


def test_parse_offset_alass_hms_format():
    """alass reports `by -0:08:34.768` (H:MM:SS.mmm, sign first) — the field format that
    falsified the first regex."""
    assert subsync._parse_offset(
        "shifted block of 1714 subtitles with length 1:27:41.420 by -0:08:34.768"
    ) == -(8 * 60 + 34.768)
    assert subsync._parse_offset("by 0:00:13.852") == 13.852
    assert subsync._parse_offset("no report here") is None


def _run_env(monkeypatch, tmp_path, offsets):
    """alass reports one offset per invocation, in order (list of alass-format strings)."""
    it = iter(offsets)

    class _Proc:
        def __init__(self, out=""):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    def fake_run(cmd, *, timeout=None, **kw):
        if cmd[0] == "ffmpeg":
            (tmp_path / "subsync-ref.wav").write_bytes(b"RIFF")
            return _Proc()
        (tmp_path / "subsync-out.srt").write_text("x")
        return _Proc(out=next(it))

    monkeypatch.setattr(subsync.util, "run_cmd", fake_run)


def test_measure_offset_consensus_median(monkeypatch, tmp_path):
    srt = tmp_path / "sub.srt"
    srt.write_text(_SRT, encoding="utf-8")
    _run_env(monkeypatch, tmp_path, ["by -0:00:13.900", "by -0:00:13.500", "by -0:00:14.200"])
    got = subsync.measure_offset(str(srt), "http://cdn/v.mkv", str(tmp_path), window_s=300)
    assert got == -13.9  # median of agreeing windows
    assert "battuta 1" in srt.read_text()  # NEVER modified


def test_measure_offset_rejects_disagreeing_windows(monkeypatch, tmp_path):
    """Field lesson #2: windows that disagree are noise — refuse, never average noise
    into a fake correction (the known +14 s file measured +0.4/-0.6/-9.0)."""
    srt = tmp_path / "sub.srt"
    srt.write_text(_SRT, encoding="utf-8")
    _run_env(monkeypatch, tmp_path, ["by 0:00:00.430", "by -0:00:00.580", "by -0:00:09.020"])
    assert subsync.measure_offset(str(srt), "http://u", str(tmp_path), window_s=300) is None


def test_measure_offset_rejects_implausible_median(monkeypatch, tmp_path):
    srt = tmp_path / "sub.srt"
    srt.write_text(_SRT, encoding="utf-8")
    _run_env(
        monkeypatch, tmp_path,
        ["by -0:06:21.000", "by -0:06:21.200", "by -0:06:20.900"],
    )  # fmt: skip
    assert subsync.measure_offset(str(srt), "http://u", str(tmp_path), max_offset_s=90.0) is None


def test_measure_offset_needs_enough_cues(monkeypatch, tmp_path):
    srt = tmp_path / "sub.srt"
    srt.write_text("1\n00:50:00,000 --> 00:50:02,000\ntardi\n", encoding="utf-8")
    monkeypatch.setattr(
        subsync.util, "run_cmd", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no run"))
    )
    assert subsync.measure_offset(str(srt), "http://u", str(tmp_path), window_s=300) is None


def test_measure_offset_extraction_failure(monkeypatch, tmp_path):
    srt = tmp_path / "sub.srt"
    srt.write_text(_SRT, encoding="utf-8")
    monkeypatch.setattr(subsync.util, "run_cmd", lambda *a, **k: None)
    assert subsync.measure_offset(str(srt), "http://u", str(tmp_path)) is None


def test_trim_shifted_rebases_to_window_start(tmp_path):
    src = tmp_path / "s.srt"
    src.write_text(_SRT, encoding="utf-8")
    parsed = subsync._read_blocks(str(src))
    out = tmp_path / "w.srt"
    kept = subsync._trim_shifted(parsed, str(out), 600.0, 900.0)  # cues in [600, 900] s
    assert kept == 26  # 12 s grid: 600..900 inclusive
    text = out.read_text()
    assert "00:00:00,000 --> 00:00:02,000" in text  # 600 s cue rebased to 0
    assert "battuta 50" in text and "battuta 76" not in text  # 76*12=912 > 900
