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
    decoded = srt.decode(str(p))
    assert decoded is not None and "perché è già là" in decoded
    vtt = srt.to_vtt(str(p))
    assert vtt is not None
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


def test_retime_drops_cues_before_zero_instead_of_piling_them(tmp_path):
    # 2026-10-01: a −4800 s shift collapsed 1018 cues onto 00:00:00,000.
    p = tmp_path / "s.srt"
    p.write_text(
        "1\n00:10:00,000 --> 00:10:02,500\nprima\n\n"
        "2\n01:19:59,000 --> 01:20:01,000\na cavallo\n\n"
        "3\n01:21:00,000 --> 01:21:02,000\ndopo\n",
        encoding="utf-8",
    )
    assert srt.retime(str(p), -4800.0, 1.0)
    cues = srt.parse_cues(p.read_text(encoding="utf-8"))
    assert [c.lines for c in cues] == [("a cavallo",), ("dopo",)]
    assert cues[0].start == 0.0 and cues[0].end == 1.0  # straddling cue keeps its tail
    assert cues[1].start == 60.0


def test_decode_cp1252_punctuation_and_utf16(tmp_path):
    p = tmp_path / "a.srt"
    p.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\n\x93Ciao\x94 \x85\n")
    assert "“Ciao” …" in (srt.decode(str(p)) or "")
    q = tmp_path / "b.srt"
    q.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nàèì\n".encode("utf-16"))
    assert len(srt.cue_spans(str(q))) == 1 and "àèì" in (srt.decode(str(q)) or "")


def test_to_vtt_emits_valid_cues(tmp_path):
    p = tmp_path / "c.srt"
    p.write_text(
        '1\n00:00:01,5 --> 00:00:02,25 X1:10 X2:20\n{\\an8}<font color="red">Ti amo <3</font>\n'
        "\n  \n"
        "2\n00:00:03,000 --> 00:00:04,000\nA --> B & <i>C</i>\n",
        encoding="utf-8",
    )
    vtt_path = srt.to_vtt(str(p))
    assert vtt_path is not None
    with open(vtt_path, encoding="utf-8") as f:
        vtt = f.read()
    assert vtt.startswith("WEBVTT\n")
    assert "00:00:01.500 --> 00:00:02.250\nTi amo &lt;3\n" in vtt  # 3-digit ms, no tags
    assert "A → B &amp; <i>C</i>" in vtt
    assert "X1:" not in vtt and "{\\an8}" not in vtt
