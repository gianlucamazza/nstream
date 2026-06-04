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
    """safety_sub_lang forces that language regardless of opts.sub_mode."""
    captured = {}
    monkeypatch.setattr(subs, "pick_subtitles", lambda *a, **k: captured.update(k) or ("x.srt",))
    opts = _opts(sub_mode=None, sub_lang=None)
    out = subs.auto_subs(CFG, "movie", "id", str(tmp_path), opts, safety_sub_lang="ita")
    assert out == ("x.srt",)
    assert captured == {"mode": "auto", "lang": "ita"}


def test_auto_subs_no_mode_returns_empty(tmp_path):
    assert subs.auto_subs(CFG, "movie", "id", str(tmp_path), _opts(sub_mode=None)) == ()
