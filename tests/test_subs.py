from pathlib import Path

import pytest

from nstream import subs
from nstream.config import Config, PlayOpts

CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])


def _opts(*, sub_mode: str | None = "auto", sub_lang: str | None = None) -> PlayOpts:
    return PlayOpts(
        auto=True, cast=False, sub_mode=sub_mode, sub_lang=sub_lang, history=True, autoplay=True
    )


@pytest.fixture
def stub_download(monkeypatch):
    """Capture the chosen subtitle and skip the real HTTP download."""
    chosen = {}

    def fake_download(sub, work_dir):
        chosen["sub"] = sub
        return f"{work_dir}/{sub.get('lang')}.srt"

    monkeypatch.setattr(subs, "_download_subtitle", fake_download)
    return chosen


def test_pick_subtitles_auto_prefers_language(monkeypatch, stub_download, tmp_path):
    tracks = [{"lang": "fre", "url": "u"}, {"lang": "ita", "url": "u"}, {"lang": "eng", "url": "u"}]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks))
    out = subs.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="auto")
    assert stub_download["sub"]["lang"] == "ita"
    assert out and out[0].endswith("ita.srt")


def test_pick_subtitles_auto_no_preferred_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: [{"lang": "fre", "url": "u"}])
    monkeypatch.setattr(
        subs, "_download_subtitle", lambda *a, **k: pytest.fail("must not download")
    )
    assert subs.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="auto") == ()


def test_pick_subtitles_lang_override(monkeypatch, stub_download, tmp_path):
    tracks = [{"lang": "ita", "url": "u"}, {"lang": "eng", "url": "u"}]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks))
    subs.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="auto", lang="eng")
    assert stub_download["sub"]["lang"] == "eng"


def test_pick_subtitles_menu_uses_fzf(monkeypatch, stub_download, tmp_path):
    tracks = [{"lang": "ita", "url": "u"}, {"lang": "eng", "url": "u"}]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks))
    monkeypatch.setattr(subs, "fzf", lambda items, prompt: items[-1][1])  # pick last
    subs.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="menu")
    assert stub_download["sub"]["lang"] == "eng"


def test_pick_subtitles_none_available(monkeypatch, tmp_path):
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: [])
    assert subs.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="auto") == ()


def test_auto_subs_safety_lang_overrides_mode(monkeypatch, tmp_path):
    """safety_sub_lang forces that language regardless of opts.sub_mode; the resolved
    stream url/filename flow through to the hash-capable pick."""
    captured = {}
    monkeypatch.setattr(
        subs, "_pick", lambda *a, **k: captured.update(k) or subs.SubsPick(("x.srt",), "lang")
    )
    opts = _opts(sub_mode=None, sub_lang=None)
    out = subs.auto_subs(
        CFG, "movie", "id", str(tmp_path), opts,
        safety_sub_lang="ita", video_url="http://u/v.mkv", filename="V.mkv",
    )  # fmt: skip
    assert out.paths == ("x.srt",) and out.match == "lang"
    assert captured == {
        "mode": "auto", "lang": "ita", "video_url": "http://u/v.mkv", "filename": "V.mkv",
    }  # fmt: skip


def test_auto_subs_no_mode_returns_empty(tmp_path):
    assert subs.auto_subs(CFG, "movie", "id", str(tmp_path), _opts(sub_mode=None)).paths == ()


def test_available_subtitle_langs_sorted_unique(monkeypatch):
    monkeypatch.setattr(
        subs.api, "subtitles",
        lambda cfg, t, v: [{"lang": "ita"}, {"lang": "eng"}, {"lang": "ita"}, {"id": "x"}],
    )  # fmt: skip
    assert subs.available_subtitle_langs(CFG, "movie", "tt1") == ["eng", "ita"]


def test_download_subtitle_sanitizes_external_lang(monkeypatch, tmp_path):
    """`lang` comes from the OpenSubtitles response: separators/traversal in it must not
    escape the per-play work dir (it lands in the mkstemp prefix)."""

    class _Resp:
        def read(self):
            return b"1\n00:00:00,000 --> 00:00:01,000\nhi\n"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(subs.urllib.request, "urlopen", lambda req, timeout: _Resp())
    out = subs._download_subtitle({"lang": "../../evil", "url": "http://x/s.srt"}, str(tmp_path))
    assert out is not None
    p = Path(out)
    assert p.parent == tmp_path  # stayed inside the work dir
    assert p.name.startswith("evil-") and p.suffix == ".srt"


def test_download_subtitle_all_bad_chars_falls_back_to_sub(monkeypatch, tmp_path):
    class _Resp:
        def read(self):
            return b"data"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(subs.urllib.request, "urlopen", lambda req, timeout: _Resp())
    out = subs._download_subtitle({"lang": "../", "url": "http://x/s.srt"}, str(tmp_path))
    assert out is not None and Path(out).name.startswith("sub-")


def test_available_subtitle_langs_network_error(monkeypatch):
    def boom(*a, **k):
        raise subs.api.NetworkError("down")

    monkeypatch.setattr(subs.api, "subtitles", boom)
    assert subs.available_subtitle_langs(CFG, "movie", "tt1") == []


# --- SRT → WebVTT conversion (for side-loaded Cast caption tracks) -----------


def test_to_vtt_converts_srt(tmp_path):
    srt = tmp_path / "eng-x.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,500\nHello\n\n", encoding="utf-8")
    vtt = subs.to_vtt(str(srt))
    assert vtt is not None and vtt.endswith(".vtt")
    body = Path(vtt).read_text(encoding="utf-8")
    assert body.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.500" in body  # comma → dot on the cue-timing line
    assert "Hello" in body


def test_to_vtt_passes_through_existing_webvtt(tmp_path):
    src = tmp_path / "eng-x.srt"
    src.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n", encoding="utf-8")
    vtt = subs.to_vtt(str(src))
    assert vtt is not None
    assert Path(vtt).read_text(encoding="utf-8").count("WEBVTT") == 1  # not double-prefixed


def test_to_vtt_missing_file_returns_none():
    assert subs.to_vtt("/nonexistent/does-not-exist.srt") is None


# --- hash-first ranking + manual retime (ADR 0018) ---------------------------


def test_pick_prefers_hash_match_within_language(monkeypatch, stub_download, tmp_path):
    """Within the preferred language a hash-matched track (timed for the exact file)
    beats the plain guesses; language stays the primary key — a hash-matched track in a
    non-preferred language must NOT win."""
    tracks = [
        {"lang": "ita", "url": "u1"},
        {"lang": "fre", "url": "u3", "hash_match": True},  # synced but wrong language
        {"lang": "ita", "url": "u2", "hash_match": True},
    ]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks))
    monkeypatch.setattr(subs.oshash, "hash_url", lambda url: ("00" * 8, 200_000))
    out = subs._pick(CFG, "movie", "id", str(tmp_path), video_url="http://u/v.mkv")
    assert stub_download["sub"]["url"] == "u2"
    assert out.match == "hash"


def test_pick_without_url_skips_hash(monkeypatch, stub_download, tmp_path):
    monkeypatch.setattr(subs.oshash, "hash_url", lambda url: pytest.fail("no url → no hash probe"))
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: [{"lang": "ita", "url": "u"}])
    out = subs._pick(CFG, "movie", "id", str(tmp_path))
    assert out.match == "lang"


def test_pick_hash_failure_degrades_to_language(monkeypatch, stub_download, tmp_path):
    """oshash.hash_url → None (no Range support / network): the pick still works as a
    plain language guess and reports it honestly."""
    seen = {}
    monkeypatch.setattr(subs.oshash, "hash_url", lambda url: None)

    def fake_subtitles(cfg, typ, vid, *, video_hash=None, video_size=0, filename=None):
        seen["hash"] = video_hash
        return [{"lang": "ita", "url": "u"}]

    monkeypatch.setattr(subs.api, "subtitles", fake_subtitles)
    out = subs._pick(CFG, "movie", "id", str(tmp_path), video_url="http://u/v.mkv")
    assert seen["hash"] is None and out.match == "lang"


_SRT = """1
00:00:10,000 --> 00:00:12,500
ciao

2
00:01:00,000 --> 00:01:02,000
mondo
"""


def test_retime_srt_offset_and_scale(tmp_path):
    p = tmp_path / "s.srt"
    p.write_text(_SRT, encoding="utf-8")
    assert subs.retime_srt(str(p), 2.0, 1.0) is True
    text = p.read_text()
    assert "00:00:12,000 --> 00:00:14,500" in text
    assert "00:01:02,000 --> 00:01:04,000" in text
    # scale: 25 fps subs on a 23.976 video stretch by 25/23.976
    p.write_text(_SRT, encoding="utf-8")
    subs.retime_srt(str(p), 0.0, 25 / 23.976)
    assert "00:00:10,427 --> 00:00:13,034" in p.read_text()


def test_retime_srt_clamps_negative_to_zero(tmp_path):
    p = tmp_path / "s.srt"
    p.write_text(_SRT, encoding="utf-8")
    subs.retime_srt(str(p), -11.0, 1.0)
    text = p.read_text()
    assert "00:00:00,000 --> 00:00:01,500" in text  # 10s cue clamped at 0
    assert "ciao" in text and "mondo" in text  # cue text untouched


def test_auto_subs_applies_retime(monkeypatch, tmp_path):
    p = tmp_path / "ita.srt"
    p.write_text(_SRT, encoding="utf-8")
    monkeypatch.setattr(subs, "_pick", lambda *a, **k: subs.SubsPick((str(p),), "hash"))
    opts = PlayOpts(
        auto=True, cast=False, sub_mode="auto", sub_lang=None, history=True, autoplay=True,
        sub_offset=-2.0,
    )  # fmt: skip
    out = subs.auto_subs(CFG, "movie", "id", str(tmp_path), opts)
    assert out.match == "hash"
    assert "00:00:08,000 --> 00:00:10,500" in p.read_text()


def test_stream_filename_reads_behavior_hints():
    assert subs.stream_filename({"behaviorHints": {"filename": "X.mkv"}}) == "X.mkv"
    assert subs.stream_filename({"behaviorHints": {}}) is None
    assert subs.stream_filename({}) is None
