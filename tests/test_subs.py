from pathlib import Path

import pytest

from nstream import subs
from nstream.config import Config, PlayOpts

CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])


@pytest.fixture(autouse=True)
def _no_host_alass(monkeypatch):
    """The audio-anchored pass (ADR 0019) gates on the HOST's alass binary: force it
    off so the suite is hermetic; wiring tests re-stub what they need."""
    monkeypatch.setattr(subs.subsync, "available", lambda: False)


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


def test_pick_skips_hash_on_loopback_url(monkeypatch, stub_download, tmp_path):
    """A loopback url is the local P2P gateway: a tail Range read would force the
    torrent's LAST piece at startup (piece-deadline anti-pattern) — no hash probe."""
    monkeypatch.setattr(
        subs.oshash, "hash_url", lambda url: pytest.fail("loopback must not be hashed")
    )
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: [{"lang": "ita", "url": "u"}])
    out = subs._pick(
        CFG, "movie", "id", str(tmp_path), video_url="http://127.0.0.1:8090/stream?link=x"
    )
    assert out.match == "lang"


# --- runtime-fit fallback (ADR 0018 refinement) -------------------------------


def _srt_ending_at(tmp_path, name, last_s):
    mm, ss = divmod(int(last_s), 60)
    hh, mm = divmod(mm, 60)
    p = tmp_path / name
    p.write_text(
        f"1\n00:00:01,000 --> 00:00:02,000\nciao\n\n"
        f"2\n{hh:02d}:{mm:02d}:{ss:02d},000 --> {hh:02d}:{mm:02d}:{ss + 1:02d},000\nfine\n",
        encoding="utf-8",
    )
    return str(p)


def _fit_env(monkeypatch, tmp_path, lasts: dict[str, float], duration: float):
    """Candidates u1..uN whose downloaded SRTs end at the given times; media lasts
    `duration` seconds."""
    files = {u: _srt_ending_at(tmp_path, f"{u}.srt", t) for u, t in lasts.items()}
    monkeypatch.setattr(subs, "_download_subtitle", lambda s, wd: files.get(s["url"]))
    monkeypatch.setattr(
        subs.tracks, "probe_tracks",
        lambda url: subs.tracks.Tracks(duration=duration, video_codec="h264"),
    )  # fmt: skip
    monkeypatch.setattr(subs.oshash, "hash_url", lambda url: None)


def test_auto_pick_fits_by_runtime(monkeypatch, tmp_path, capsys):
    """Live regression (Coherence, 2026-07-16): two timing clusters, the addon's arbitrary
    order picked the one 80 s short of the file → visible desync. With no protocol hash
    match, the candidate whose last cue fits the REAL media duration must win."""
    _fit_env(
        monkeypatch, tmp_path,
        {"u1": 5197.0, "u2": 5263.0, "u3": 5277.0}, duration=5276.4,
    )  # fmt: skip
    tracks_list = [{"lang": "ita", "url": u} for u in ("u1", "u2", "u3")]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks_list))
    out = subs._pick(CFG, "movie", "id", str(tmp_path), video_url="http://cdn/v.mkv")
    assert out.match == "runtime"
    assert out.paths[0].endswith("u3.srt")  # 5277.0 vs 5276.4: the fitting cluster
    assert "aderenza al runtime" in capsys.readouterr().err


def test_auto_pick_runtime_gap_too_large_is_honest_lang(monkeypatch, tmp_path, capsys):
    """Every candidate far from the media duration → still delivered (better than
    nothing) but reported as a plain guess, with a warning."""
    _fit_env(monkeypatch, tmp_path, {"u1": 4000.0, "u2": 4100.0}, duration=5276.4)
    tracks_list = [{"lang": "ita", "url": "u1"}, {"lang": "ita", "url": "u2"}]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks_list))
    out = subs._pick(CFG, "movie", "id", str(tmp_path), video_url="http://cdn/v.mkv")
    assert out.match == "lang" and out.paths[0].endswith("u2.srt")
    assert "non garantita" in capsys.readouterr().err


def test_auto_pick_true_hash_match_skips_runtime_probe(monkeypatch, tmp_path):
    """A protocol-verified hash match (m=='h' → hash_match) is synced by construction:
    no candidate downloads beyond it, no duration probe."""
    monkeypatch.setattr(
        subs.tracks, "probe_tracks", lambda url: pytest.fail("hash match → no probe")
    )
    monkeypatch.setattr(subs.oshash, "hash_url", lambda url: ("00" * 8, 200_000))
    monkeypatch.setattr(subs, "_download_subtitle", lambda s, wd: str(tmp_path / "x.srt"))
    tracks_list = [
        {"lang": "ita", "url": "u1", "hash_match": True},
        {"lang": "ita", "url": "u2"},
    ]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks_list))
    out = subs._pick(CFG, "movie", "id", str(tmp_path), video_url="http://cdn/v.mkv")
    assert out.match == "hash"


# --- audio-anchored correction wiring (ADR 0019) ------------------------------


CFG_AS = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"], sub_autosync=True)


def _autosync_env(monkeypatch, tmp_path, *, offset=-13.9, match="runtime"):
    p = tmp_path / "ita.srt"
    p.write_text("1\n00:00:20,000 --> 00:00:21,000\nciao\n")
    monkeypatch.setattr(subs, "_pick", lambda *a, **k: subs.SubsPick((str(p),), match))
    monkeypatch.setattr(subs.subsync, "available", lambda: True)
    calls: list[tuple] = []
    monkeypatch.setattr(
        subs.subsync, "measure_offset",
        lambda srt, url, wd, window_s: calls.append((srt, url, window_s)) or offset,
    )  # fmt: skip
    return calls, p


def test_auto_subs_audio_corrects_non_hash_pick(monkeypatch, tmp_path, capsys):
    calls, srt = _autosync_env(monkeypatch, tmp_path)
    out = subs.auto_subs(
        CFG_AS, "movie", "id", str(tmp_path), _opts(), video_url="http://cdn/v.mkv"
    )
    assert out.match == "audio" and len(calls) == 1
    assert calls[0][1] == "http://cdn/v.mkv" and calls[0][2] == CFG_AS.sub_autosync_window_s
    assert "allineati all'audio (offset -13.9s)" in capsys.readouterr().err
    # the measured offset was applied by retime_srt to the INTACT original (20s → 6.1s)
    assert "00:00:06,100 --> 00:00:07,100" in srt.read_text()


def test_auto_subs_audio_zero_offset_verifies_without_rewrite(monkeypatch, tmp_path, capsys):
    calls, srt = _autosync_env(monkeypatch, tmp_path, offset=0.2)
    before = srt.read_text()
    out = subs.auto_subs(CFG_AS, "movie", "id", str(tmp_path), _opts(), video_url="http://u")
    assert out.match == "audio" and srt.read_text() == before  # verified, not rewritten


def test_auto_subs_audio_skips_hash_match(monkeypatch, tmp_path):
    calls, _ = _autosync_env(monkeypatch, tmp_path, match="hash")
    out = subs.auto_subs(CFG_AS, "movie", "id", str(tmp_path), _opts(), video_url="http://u")
    assert out.match == "hash" and calls == []  # synced by construction → no audio pass


def test_auto_subs_audio_skips_on_manual_retime(monkeypatch, tmp_path):
    """--sub-offset/--sub-fps are a user override: the auto pass must step aside."""
    calls, _ = _autosync_env(monkeypatch, tmp_path)
    opts = PlayOpts(
        auto=True, cast=False, sub_mode="auto", sub_lang=None, history=True, autoplay=True,
        sub_offset=-14.0,
    )  # fmt: skip
    out = subs.auto_subs(CFG_AS, "movie", "id", str(tmp_path), opts, video_url="http://u")
    assert calls == [] and out.match == "runtime"  # manual retime applied, match honest


def test_auto_subs_audio_failure_keeps_honest_match(monkeypatch, tmp_path, capsys):
    _autosync_env(monkeypatch, tmp_path, offset=None)
    out = subs.auto_subs(CFG_AS, "movie", "id", str(tmp_path), _opts(), video_url="http://u")
    assert out.match == "runtime"  # not upgraded: the correction did not run
    assert "allineati all'audio" not in capsys.readouterr().err


def test_auto_subs_audio_off_by_default(monkeypatch, tmp_path):
    """sub_autosync defaults to OFF (field-falsified windowed measurement): even with
    alass present, the default config must not run the audio pass."""
    calls, _ = _autosync_env(monkeypatch, tmp_path)
    out = subs.auto_subs(CFG, "movie", "id", str(tmp_path), _opts(), video_url="http://u")
    assert calls == [] and out.match == "runtime"
