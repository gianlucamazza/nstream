"""Unit tests for the subtitle text-format toolbox (decode/retime/to_vtt/cue_spans)."""

from __future__ import annotations

from pathlib import Path

from nstream import srt

_SRT = """1
00:00:10,000 --> 00:00:12,500
ciao

2
00:01:00,000 --> 00:01:02,000
mondo
"""


def test_to_vtt_converts_srt(tmp_path):
    path = tmp_path / "eng-x.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,500\nHello\n\n", encoding="utf-8")
    vtt = srt.to_vtt(str(path))
    assert vtt is not None and vtt.endswith(".vtt")
    body = Path(vtt).read_text(encoding="utf-8")
    assert body.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in body  # comma → dot on the cue-timing line
    assert "Hello" in body


def test_to_vtt_passes_through_existing_webvtt(tmp_path):
    src = tmp_path / "eng-x.srt"
    src.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n", encoding="utf-8")
    vtt = srt.to_vtt(str(src))
    assert vtt is not None
    assert Path(vtt).read_text(encoding="utf-8").count("WEBVTT") == 1  # not double-prefixed


def test_to_vtt_missing_file_returns_none():
    assert srt.to_vtt("/nonexistent/does-not-exist.srt") is None


# --- hash-first ranking + manual retime (ADR 0018) ---------------------------


def test_retime_srt_offset_and_scale(tmp_path):
    p = tmp_path / "s.srt"
    p.write_text(_SRT, encoding="utf-8")
    assert srt.retime(str(p), 2.0, 1.0) is True
    text = p.read_text()
    assert "00:00:12,000 --> 00:00:14,500" in text
    assert "00:01:02,000 --> 00:01:04,000" in text
    # scale: 25 fps subs on a 23.976 video stretch by 25/23.976
    p.write_text(_SRT, encoding="utf-8")
    srt.retime(str(p), 0.0, 25 / 23.976)
    assert "00:00:10,427 --> 00:00:13,034" in p.read_text()


def test_retime_srt_clamps_negative_to_zero(tmp_path):
    p = tmp_path / "s.srt"
    p.write_text(_SRT, encoding="utf-8")
    srt.retime(str(p), -11.0, 1.0)
    text = p.read_text()
    assert "00:00:00,000 --> 00:00:01,500" in text  # 10s cue clamped at 0
    assert "ciao" in text and "mondo" in text  # cue text untouched


def test_decode_sub_preserves_latin1_accents(tmp_path):
    """A CP1252/latin-1 Italian track must keep its accents — errors="replace" used to
    mojibake every `è`/`à` on the TV (the addon's SubEncoding field is unreliable)."""
    p = tmp_path / "s.srt"
    p.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nperché è già là\n".encode("latin-1"))
    assert "perché è già là" in srt.decode(str(p))
    vtt = srt.to_vtt(str(p))
    text = Path(vtt).read_text(encoding="utf-8")  # VTT spec REQUIRES UTF-8
    assert "perché è già là" in text and text.startswith("WEBVTT")


def test_retime_srt_transcodes_latin1_to_utf8(tmp_path):
    p = tmp_path / "s.srt"
    p.write_bytes("1\n00:00:01,000 --> 00:00:02,000\ncosì\n".encode("latin-1"))
    assert srt.retime(str(p), 1.0, 1.0) is True
    text = p.read_text(encoding="utf-8")
    assert "così" in text and "00:00:02,000 --> 00:00:03,000" in text


def test_cue_spans_parses_intervals(tmp_path):
    p = tmp_path / "s.srt"
    p.write_text(_SRT, encoding="utf-8")
    spans = srt.cue_spans(str(p))
    assert spans == ((10.0, 12.5), (60.0, 62.0))


def test_cue_spans_skips_malformed_and_accepts_dot_separator(tmp_path):
    p = tmp_path / "s.srt"
    p.write_text(
        "1\n00:00:01.000 --> 00:00:02.500\nok dot\n\n"
        "2\nnot a timing line\n\n"
        "3\n00:00:05,000 --> 00:00:04,000\nend before start: skipped\n",
        encoding="utf-8",
    )
    assert srt.cue_spans(str(p)) == ((1.0, 2.5),)


def test_cue_spans_unreadable_returns_empty(tmp_path):
    assert srt.cue_spans(str(tmp_path / "missing.srt")) == ()
