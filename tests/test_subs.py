from pathlib import Path

import pytest

from nstream import subs
from nstream.config import Config, PlayOpts
from nstream.types import Subtitle

CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])


@pytest.fixture(autouse=True)
def _no_host_engine(monkeypatch):
    """The local-media alignment tier gates on the HOST's ffmpeg: force the engine off
    so the suite is hermetic; wiring tests re-stub what they need."""
    monkeypatch.setattr(subs.subalign, "available", lambda: False)


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


def test_pick_subtitles_menu_uses_injected_choose(monkeypatch, stub_download, tmp_path):
    tracks = [{"lang": "ita", "url": "u"}, {"lang": "eng", "url": "u"}]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks))
    pick_last = lambda items, prompt: items[-1][1]  # noqa: E731
    subs.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="menu", choose=pick_last)
    assert stub_download["sub"]["lang"] == "eng"


def test_menu_mode_without_choose_falls_back_to_auto(monkeypatch, stub_download, tmp_path):
    # ADR 0037: no injected menu (e.g. --json) never opens one; the automatic pick applies.
    tracks = [{"lang": "ita", "url": "u"}, {"lang": "eng", "url": "u"}]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks))
    subs.pick_subtitles(CFG, "movie", "id", str(tmp_path), mode="menu")
    assert stub_download["sub"]["lang"] == "ita"


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
        def read(self, n=-1):
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
        def read(self, n=-1):
            return b"data"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(subs.urllib.request, "urlopen", lambda req, timeout: _Resp())
    out = subs._download_subtitle({"lang": "../", "url": "http://x/s.srt"}, str(tmp_path))
    assert out is not None and Path(out).name.startswith("sub-")


class _Body:
    def __init__(self, data: bytes):
        self.data = data
        self.calls = 0

    def __call__(self, req, timeout):
        self.calls += 1
        return self

    def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_subtitle_caches_by_url(monkeypatch, tmp_path):
    """A second download of the same URL (re-cast, alignment alternates) hits the cache,
    and each call gets its own work-dir copy (retime rewrites it in place)."""
    body = _Body(b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    monkeypatch.setattr(subs.urllib.request, "urlopen", body)
    sub: Subtitle = {"lang": "ita", "url": "http://x/s.srt"}
    a = subs._download_subtitle(sub, str(tmp_path))
    b = subs._download_subtitle(sub, str(tmp_path))
    assert body.calls == 1
    assert a and b and a != b and Path(a).read_bytes() == Path(b).read_bytes() == body.data


def test_download_subtitle_rejects_oversize(monkeypatch, tmp_path):
    monkeypatch.setattr(subs, "_SUB_MAX_BYTES", 10)
    monkeypatch.setattr(subs.urllib.request, "urlopen", _Body(b"x" * 11))
    assert subs._download_subtitle({"url": "http://x/big.srt"}, str(tmp_path)) is None


def test_download_subtitle_rejects_gzip_bomb(monkeypatch, tmp_path):
    import gzip

    monkeypatch.setattr(subs, "_SUB_MAX_DECODED", 1000)
    monkeypatch.setattr(subs.urllib.request, "urlopen", _Body(gzip.compress(b"\0" * 5000)))
    assert subs._download_subtitle({"url": "http://x/s.srt.gz"}, str(tmp_path)) is None


def test_download_subtitle_gunzips(monkeypatch, tmp_path):
    import gzip

    monkeypatch.setattr(subs.urllib.request, "urlopen", _Body(gzip.compress(b"hello")))
    out = subs._download_subtitle({"url": "http://x/s.gz"}, str(tmp_path))
    assert out is not None and Path(out).read_bytes() == b"hello"


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


# --- evidence-tier selection + local-media alignment (ADR 0020) ---------------


def test_choose_lang_guess_carries_alternates(monkeypatch, stub_download, tmp_path):
    """Tier 3 delivers the first candidate honestly as "lang" and keeps the rest of the
    same-language pool as alternates for the local-alignment tier."""
    tracks_list = [{"lang": "ita", "url": f"u{i}"} for i in range(8)]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks_list))
    out = subs._pick(CFG, "movie", "id", str(tmp_path))
    assert out.match == "lang" and stub_download["sub"]["url"] == "u0"
    assert [a["url"] for a in out.alternates] == ["u1", "u2", "u3", "u4", "u5"]  # cap 6


def test_choose_hash_match_has_no_alternates(monkeypatch, stub_download, tmp_path):
    tracks_list = [{"lang": "ita", "url": "u0", "hash_match": True}, {"lang": "ita", "url": "u1"}]
    monkeypatch.setattr(subs.api, "subtitles", lambda *a, **k: list(tracks_list))
    out = subs._pick(CFG, "movie", "id", str(tmp_path))
    assert out.match == "hash" and out.alternates == ()


def _opts_plain(**kw):
    from dataclasses import replace

    return replace(
        PlayOpts(auto=True, cast=True, sub_mode="auto", sub_lang=None, history=True, autoplay=True),
        **kw,
    )


def _srt_file(tmp_path, name="ita.srt"):
    p = tmp_path / name
    p.write_text("1\n00:00:20,000 --> 00:00:21,000\nciao\n", encoding="utf-8")
    return p


def _align_env(monkeypatch, *, verdict_offset=-13.9, reason="aligned", duration=5276.4):
    from nstream import subalign

    monkeypatch.setattr(subs.subalign, "available", lambda: True)
    monkeypatch.setattr(
        subs.tracks, "probe_tracks",
        lambda path: subs.tracks.Tracks(duration=duration),
    )  # fmt: skip
    calls = {"probe": [], "align": []}
    fp = subalign.Fingerprint(duration, ((0.0, 100.0),), ((1.0, 2.0),))
    monkeypatch.setattr(
        subs.subalign, "probe_local",
        lambda path, *, duration, timeout_s=180.0, **_k: calls["probe"].append(path) or fp,
    )  # fmt: skip
    diag = subalign.Alignment(verdict_offset or 0.0, 0.9, 0.2, 100.0, 50, 0.1, 0.2, 6, 8)
    monkeypatch.setattr(
        subs.subalign, "align",
        lambda spans, f: calls["align"].append(spans)
        or subalign.Verdict(verdict_offset, 1.0, reason, diag),
    )  # fmt: skip
    return calls


def test_align_local_corrects_lang_pick(monkeypatch, tmp_path, capsys):
    srt_path = _srt_file(tmp_path)
    calls = _align_env(monkeypatch, verdict_offset=-13.9)
    pick = subs.SubsPick((str(srt_path),), "lang")
    out = subs.align_local(CFG, pick, "/media/remux.mp4", str(tmp_path), _opts_plain())
    assert out.match == "audio" and out.offset_s == -13.9
    assert calls["probe"] == ["/media/remux.mp4"] and len(calls["align"]) == 1
    assert "00:00:06,100 --> 00:00:07,100" in srt_path.read_text()  # -13.9 applied
    assert "allineati all'audio del file (offset -13.9s)" in capsys.readouterr().err


def test_align_local_zero_offset_verifies_without_rewrite(monkeypatch, tmp_path):
    srt_path = _srt_file(tmp_path)
    before = srt_path.read_text()
    _align_env(monkeypatch, verdict_offset=0.2)
    out = subs.align_local(
        CFG, subs.SubsPick((str(srt_path),), "lang"), "/m.mp4", str(tmp_path), _opts_plain()
    )
    assert out.match == "audio" and srt_path.read_text() == before


def test_align_local_refusal_keeps_honest_lang(monkeypatch, tmp_path):
    srt_path = _srt_file(tmp_path)
    _align_env(monkeypatch, verdict_offset=None, reason="cross_window_disagree")
    pick = subs.SubsPick((str(srt_path),), "lang")
    out = subs.align_local(CFG, pick, "/m.mp4", str(tmp_path), _opts_plain())
    assert out is pick  # unchanged, honest "lang"


def test_align_local_falls_back_to_alternate(monkeypatch, tmp_path, stub_download):
    """A refusing delivered track tries the alternates: a garbage SRT refuses, the next
    family may align (the engine's verdict decides, not the addon's order)."""
    from nstream import subalign

    bad = _srt_file(tmp_path, "bad.srt")
    _align_env(monkeypatch)  # base env; override align below

    def fake_download(sub, wd):
        p = tmp_path / f"{sub['url']}.srt"
        p.write_text("1\n00:00:20,000 --> 00:00:21,000\nalt\n", encoding="utf-8")
        return str(p)

    monkeypatch.setattr(subs, "_download_subtitle", fake_download)
    diag = subalign.Alignment(0.0, 0.9, 0.2, 100.0, 50, 0.1, 0.2, 6, 8)
    attempted = False

    def fake_align(spans, fp):
        nonlocal attempted
        # first call (delivered) refuses; second (alternate) aligns at -7
        if not attempted:
            attempted = True
            return subalign.Verdict(None, 1.0, "low_score", diag)
        return subalign.Verdict(-7.0, 1.0, "aligned", diag)

    monkeypatch.setattr(subs.subalign, "align", fake_align)
    pick = subs.SubsPick((str(bad),), "lang", alternates=({"lang": "ita", "url": "alt1"},))
    out = subs.align_local(CFG, pick, "/m.mp4", str(tmp_path), _opts_plain())
    assert out.match == "audio" and out.offset_s == -7.0
    assert out.paths[0].endswith("alt1.srt")


def test_align_local_skips_hash_and_manual_and_disabled(monkeypatch, tmp_path):
    srt_path = _srt_file(tmp_path)
    calls = _align_env(monkeypatch)
    hash_pick = subs.SubsPick((str(srt_path),), "hash")
    assert subs.align_local(CFG, hash_pick, "/m", str(tmp_path), _opts_plain()) is hash_pick
    lang_pick = subs.SubsPick((str(srt_path),), "lang")
    manual = _opts_plain(sub_offset=-14.0)
    assert subs.align_local(CFG, lang_pick, "/m", str(tmp_path), manual) is lang_pick
    off = Config(torrentio_base="tb", sub_align=False)
    assert subs.align_local(off, lang_pick, "/m", str(tmp_path), _opts_plain()) is lang_pick
    assert calls["probe"] == []  # nessun probe in tutti e tre i casi


def test_align_local_timeout_scales_with_runtime_and_size(monkeypatch, tmp_path):
    """The probe decodes one downmixed channel (~4 ms per second of runtime) after
    demuxing the file: the timeout grows with both, the config budget as the floor. The
    source's channel count reaches the probe so a 5.1 mix is measured on its centre."""
    srt_path = _srt_file(tmp_path)
    _align_env(monkeypatch, duration=8526.0)
    monkeypatch.setattr(
        subs.tracks, "probe_tracks",
        lambda path: subs.tracks.Tracks(
            duration=8526.0, audio=[subs.tracks.Track(1, "eng", "aac", 6)]
        ),
    )  # fmt: skip
    seen = {}
    monkeypatch.setattr(
        subs.subalign, "probe_local",
        lambda path, *, duration, timeout_s, channels=None: seen.update(t=timeout_s, ch=channels)
        or subs.subalign.Fingerprint(8526.0, ((0.0, 100.0),), ((1.0, 2.0),)),
    )  # fmt: skip
    monkeypatch.setattr(subs.os.path, "getsize", lambda p: 36_000_000_000)  # 36 GB
    pick = subs.SubsPick((str(srt_path),), "lang")
    subs.align_local(CFG, pick, "/m.mp4", str(tmp_path), _opts_plain())
    assert seen["ch"] == 6
    assert seen["t"] >= 8526.0 * 0.02 + 36_000_000_000 / 100e6  # runtime + demux headroom


def test_report_safety_subs_only_on_real_outcome(capsys):
    """The "attivati" line must follow the evidence, never the intent.

    Regression: the audio-language decision site printed "sottotitoli X attivati" before
    the fetch ran, so a run where OpenSubtitles returned nothing emitted both that line
    and "nessun sottotitolo nelle lingue preferite".
    """
    subs.report_safety_subs(subs.SubsPick(paths=("/tmp/a.srt",), match="lang"), "ita")
    assert "sottotitoli ita attivati" in capsys.readouterr().err
    # Nothing acquired → say nothing (auto_subs already reported the empty outcome).
    subs.report_safety_subs(subs.SubsPick(), "ita")
    assert capsys.readouterr().err == ""
    # No safety language requested → nothing to report either way.
    subs.report_safety_subs(subs.SubsPick(paths=("/tmp/a.srt",), match="hash"), None)
    assert capsys.readouterr().err == ""


def test_choose_moves_on_when_a_download_fails(monkeypatch, tmp_path):
    # A failed download used to end the pick with the alternates unused.
    pool: list[Subtitle] = [
        {"lang": "ita", "url": "u1"},
        {"lang": "ita", "url": "u2"},
        {"lang": "ita", "url": "u3"},
    ]
    got = {"u1": None, "u2": str(tmp_path / "2.srt"), "u3": str(tmp_path / "3.srt")}
    monkeypatch.setattr(subs, "_download_subtitle", lambda s, wd: got[s["url"]])
    pick = subs._choose(pool, {"ita": 0}, str(tmp_path))
    assert pick is not None and pick.paths == (got["u2"],) and pick.lang == "ita"
    assert [a["url"] for a in pick.alternates] == ["u3"]


@pytest.mark.parametrize(("match", "said"), [("lang", True), ("audio", False), ("hash", False)])
def test_report_unverified_only_for_language_guesses(match, said):
    with subs.notices.capture() as bag:
        subs.report_unverified(subs.SubsPick(("/s.srt",), match), hint="h")
        subs.report_unverified(subs.SubsPick(), hint="h")  # nothing delivered: silent
    assert [n.code for n in bag] == (["subs_unverified"] if said else [])


def test_embedded_pick_skips_forced_and_bitmap_tracks():
    """In the Mood for Love (field 2026-10-02): [ita forced, ita, eng, …] → the full ita."""
    from nstream.tracks import Track, Tracks

    tr = Tracks(subs=[
        Track(1, "ita", "subrip", forced=True), Track(2, "ita", "subrip"),
        Track(3, "eng", "subrip"), Track(4, "fre", "hdmv_pgs_subtitle"),
    ])  # fmt: skip
    assert subs.embedded_pick(tr, ["ita", "eng"]) == (1, "ita")
    assert subs.embedded_pick(tr, ["fra"]) is None  # only a bitmap French track
    assert subs.embedded_pick(tr, ["eng"]) == (2, "eng")
    two_letter = Tracks(subs=[Track(1, "it", "ass")])
    assert subs.embedded_pick(two_letter, ["ita"]) == (0, "ita")
