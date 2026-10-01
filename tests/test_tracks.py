"""Unit tests for ffprobe track parsing and the graceful-fallback probe."""

from __future__ import annotations

import pytest

from nstream import tracks


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The per-url probe memo must not leak between tests."""
    tracks.clear_cache()
    yield
    tracks.clear_cache()


FFPROBE_JSON = {
    "format": {"duration": "5400.25"},
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
    assert tr.n_video == 1  # container metadata is still collected


def test_parse_collects_n_video_and_duration():
    tr = tracks._parse_ffprobe(FFPROBE_JSON)
    assert tr.n_video == 1
    assert tr.duration == pytest.approx(5400.25)


def test_parse_duration_defaults_zero_when_unparseable():
    tr = tracks._parse_ffprobe({"format": {"duration": "n/a"}, "streams": []})
    assert tr.duration == 0.0 and tr.n_video == 0


def test_probe_tracks_missing_or_failed_ffprobe(monkeypatch):
    # run_cmd returns None when the binary is missing or the probe times out.
    monkeypatch.setattr(tracks.util, "run_cmd", lambda *a, **k: None)
    assert tracks.probe_tracks("http://u").empty()


def test_probe_tracks_parses_stdout(monkeypatch):
    import json

    class _P:
        stdout = json.dumps(FFPROBE_JSON)

    monkeypatch.setattr(tracks.util, "run_cmd", lambda *a, **k: _P())
    tr = tracks.probe_tracks("http://u")
    assert len(tr.audio) == 2 and len(tr.subs) == 2
    assert tr.n_video == 1 and tr.duration == pytest.approx(5400.25)


def test_probe_tracks_memoized_per_url(monkeypatch):
    import json

    calls = []

    class _P:
        stdout = json.dumps(FFPROBE_JSON)

    def run_cmd(*a, **k):
        calls.append(a)
        return _P()

    monkeypatch.setattr(tracks.util, "run_cmd", run_cmd)
    first = tracks.probe_tracks("http://u")
    second = tracks.probe_tracks("http://u")
    assert len(calls) == 1  # second probe of the same url is a cache hit
    assert second is first
    tracks.probe_tracks("http://other")
    assert len(calls) == 2  # a different url still probes


def test_probe_tracks_caches_failures_too(monkeypatch):
    calls = []

    def run_cmd(*a, **k):
        calls.append(a)
        return None  # ffprobe missing / timed out

    monkeypatch.setattr(tracks.util, "run_cmd", run_cmd)
    assert tracks.probe_tracks("http://dead").empty()
    assert tracks.probe_tracks("http://dead").empty()
    assert len(calls) == 1  # a failed url never re-pays the probe timeout in-process


def test_clear_cache_forces_a_new_probe(monkeypatch):
    calls = []

    def run_cmd(*a, **k):
        calls.append(a)
        return None

    monkeypatch.setattr(tracks.util, "run_cmd", run_cmd)
    tracks.probe_tracks("http://u")
    tracks.clear_cache()
    tracks.probe_tracks("http://u")
    assert len(calls) == 2


def test_parse_captures_first_video_codec():
    """The cast video vetting (ADR 0017) reads the real codec from the same probe."""
    assert tracks._parse_ffprobe(FFPROBE_JSON).video_codec == "h264"


def test_parse_video_codec_empty_without_video_stream():
    assert tracks._parse_ffprobe({"streams": []}).video_codec == ""


def test_parse_captures_container_format_name():
    """The cast container vetting (ADR 0022) reads the raw ffprobe format_name from the same
    probe (to confirm a lying filename extension)."""
    data = {"format": {"duration": "1.0", "format_name": "matroska,webm"}, "streams": []}
    assert tracks._parse_ffprobe(data).container == "matroska,webm"
    mp4 = {"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}, "streams": []}
    assert tracks._parse_ffprobe(mp4).container == "mov,mp4,m4a,3gp,3g2,mj2"
    assert tracks._parse_ffprobe({"streams": []}).container == ""


def test_probe_tracks_requests_format_name(monkeypatch):
    """The ffprobe argv asks for format_name (so the container rides the memoized probe)."""
    seen = {}

    def fake_run(cmd, timeout=None):
        seen["cmd"] = cmd
        return None  # ffprobe "missing" → empty Tracks, we only assert on the argv

    monkeypatch.setattr(tracks.util, "run_cmd", fake_run)
    tracks.probe_tracks("http://x/a.mkv")
    entries = seen["cmd"][seen["cmd"].index("-show_entries") + 1]
    assert "format_name" in entries


def test_probe_tracks_is_bounded(monkeypatch):
    # P3: a stalled remote read must fail fast, and probing stays on the headers.
    seen = []
    monkeypatch.setattr(tracks.util, "run_cmd", lambda cmd, **k: seen.append(cmd))
    tracks._cache.clear()
    tracks.probe_tracks("http://x/realdebrid=TOK/bounded.mkv")
    cmd = seen[0]
    assert cmd[cmd.index("-rw_timeout") + 1] == "6000000"
    assert cmd[cmd.index("-probesize") + 1] == "2M"
    # The debrid url (and its token) never reaches argv: ffprobe reads a loopback proxy.
    assert not any("TOK" in a for a in cmd)
    assert cmd[-1].startswith("http://127.0.0.1:")
