"""Unit tests for ffprobe track parsing and the graceful-fallback probe."""

from __future__ import annotations

from nstream import tracks

FFPROBE_JSON = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "tags": {"language": "eng", "title": "Original"}},
        {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6,
         "tags": {"language": "ita"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "eng", "title": "SDH"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "subrip"},
    ]
}  # fmt: skip


def test_parse_assigns_per_type_1based_ids():
    tr = tracks._parse_ffprobe(FFPROBE_JSON)
    assert [a.id for a in tr.audio] == [1, 2]  # video doesn't shift audio numbering
    assert [s.id for s in tr.subs] == [1, 2]
    assert tr.audio[0].lang == "eng" and tr.audio[0].channels == 2
    assert tr.audio[1].lang == "ita" and tr.audio[1].codec == "eac3" and tr.audio[1].channels == 6


def test_parse_defaults_language_und_when_untagged():
    tr = tracks._parse_ffprobe(FFPROBE_JSON)
    assert tr.subs[1].lang == "und" and tr.subs[1].title == ""


def test_parse_empty_when_no_av_tracks():
    tr = tracks._parse_ffprobe({"streams": [{"codec_type": "video", "codec_name": "h264"}]})
    assert tr.empty()


def test_probe_tracks_missing_ffprobe(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(tracks.subprocess, "run", boom)
    assert tracks.probe_tracks("http://u").empty()


def test_probe_tracks_timeout(monkeypatch):
    import subprocess as sp

    def slow(*a, **k):
        raise sp.TimeoutExpired(cmd="ffprobe", timeout=1)

    monkeypatch.setattr(tracks.subprocess, "run", slow)
    assert tracks.probe_tracks("http://u").empty()


def test_probe_tracks_parses_stdout(monkeypatch):
    import json

    class _P:
        stdout = json.dumps(FFPROBE_JSON)

    monkeypatch.setattr(tracks.subprocess, "run", lambda *a, **k: _P())
    tr = tracks.probe_tracks("http://u")
    assert len(tr.audio) == 2 and len(tr.subs) == 2
