"""Unit tests for stream selection, resolution, and the auto-play vetting guards.

Patches are applied on the `stream_select` module (where the helpers are looked up),
not on `cli` — the orchestrator only calls `prepare_stream`/`pick_and_resolve`."""

from __future__ import annotations

import pytest

from nstream import stream_select
from nstream.config import Config, PlayOpts, Stream


def _gopts(*, cast: bool = False, quality: int | None = 0) -> PlayOpts:
    # quality=0 (Auto) by default so unit tests don't open the in-flow quality picker.
    return PlayOpts(
        auto=True,
        cast=cast,
        sub_mode=None,
        sub_lang=None,
        history=False,
        autoplay=False,
        quality=quality,
    )


@pytest.fixture(autouse=True)
def _clear_probe_memo():
    """The reachability probe memo is process-lifetime — clear it between tests so a url's
    verdict from one test can't leak into another that stubs `url_playable` differently."""
    stream_select._PROBE_MEMO.clear()
    yield
    stream_select._PROBE_MEMO.clear()


# --- release date message --------------------------------------------------


def test_future_release_parsing():
    assert stream_select._future_release("2999-12-18T00:00:00.000Z") is not None  # far future
    assert stream_select._future_release("2000-01-01T00:00:00.000Z") is None  # past
    assert stream_select._future_release(None) is None
    assert stream_select._future_release("not-a-date") is None


def test_no_streams_message_upcoming(monkeypatch):
    monkeypatch.setattr(
        stream_select.api, "meta", lambda *a, **k: {"released": "2999-12-18T00:00:00.000Z"}
    )
    msg = stream_select.no_streams_message(Config(torrentio_base="tb"), "movie", "tt1", "Dune 3")
    assert "non ancora disponibile" in msg and "18/12/2999" in msg


def test_no_streams_message_released(monkeypatch):
    monkeypatch.setattr(
        stream_select.api, "meta", lambda *a, **k: {"released": "2000-01-01T00:00:00.000Z"}
    )
    msg = stream_select.no_streams_message(Config(torrentio_base="tb"), "movie", "tt1", "Old Film")
    assert "nessuno stream disponibile" in msg


# --- native debrid wiring --------------------------------------------------


class _FakeResolver:
    name = "torbox"
    marker = "TB"

    def __init__(self, *, cached=(), resolved="http://cdn/x.mkv", fail=False):
        self._cached = {h.lower() for h in cached}
        self._resolved = resolved
        self._fail = fail

    def cached(self, hashes):
        return {h.lower() for h in hashes if h.lower() in self._cached}

    def resolve(self, stream):
        if self._fail:
            raise stream_select.debrid.DebridUnavailable("boom")
        return self._resolved


def _native_cfg() -> Config:
    return Config(torrentio_base="sort=qualitysize|torbox=TOK", playback_backend="native")


def test_mark_native_cached_prefixes_only_cached(monkeypatch):
    monkeypatch.setattr(
        stream_select.debrid, "get_resolver", lambda cfg: _FakeResolver(cached=["aaaa"])
    )
    results: list[Stream] = [
        {"name": "Movie A", "infoHash": "AAAA"},
        {"name": "Movie B", "infoHash": "bbbb"},
    ]
    stream_select._mark_native_cached(_native_cfg(), results)
    assert results[0]["name"].startswith("[TB+]")
    assert not results[1]["name"].startswith("[TB+]")
    stream_select._mark_native_cached(_native_cfg(), results)  # idempotent
    assert results[0]["name"].count("[TB+]") == 1


def test_mark_native_cached_noop_off_backend(monkeypatch):
    called = []
    monkeypatch.setattr(stream_select.debrid, "get_resolver", lambda cfg: called.append(1))
    results: list[Stream] = [{"name": "X", "infoHash": "aaaa"}]
    stream_select._mark_native_cached(Config(playback_backend="local"), results)
    assert results[0]["name"] == "X" and not called


def test_native_resolve_returns_url(monkeypatch):
    monkeypatch.setattr(
        stream_select.debrid, "get_resolver", lambda cfg: _FakeResolver(resolved="http://cdn/y.mkv")
    )
    assert stream_select._native_resolve(_native_cfg(), {"infoHash": "aaaa"}) == "http://cdn/y.mkv"


def test_native_resolve_none_off_backend():
    assert (
        stream_select._native_resolve(Config(playback_backend="local"), {"infoHash": "a"}) is None
    )


def test_resolve_stream_native_first(monkeypatch):
    monkeypatch.setattr(
        stream_select.debrid, "get_resolver", lambda cfg: _FakeResolver(resolved="http://cdn/z.mkv")
    )
    monkeypatch.setattr(
        stream_select.engine, "resolve", lambda *a, **k: pytest.fail("engine must not be called")
    )
    out = stream_select._resolve_stream(_native_cfg(), {"infoHash": "aaaa"})
    assert out is not None and out["url"] == "http://cdn/z.mkv"


def test_resolve_stream_falls_back_to_p2p(monkeypatch):
    monkeypatch.setattr(stream_select.debrid, "get_resolver", lambda cfg: _FakeResolver(fail=True))
    monkeypatch.setattr(stream_select, "_p2p_guard", lambda cfg: True)
    monkeypatch.setattr(stream_select.engine, "resolve", lambda cfg, s: "http://127.0.0.1:8090/s")
    out = stream_select._resolve_stream(_native_cfg(), {"infoHash": "aaaa"})
    assert out is not None and out["url"] == "http://127.0.0.1:8090/s"


def test_playable_url_native(monkeypatch):
    monkeypatch.setattr(
        stream_select.debrid, "get_resolver", lambda cfg: _FakeResolver(resolved="http://cdn/p.mkv")
    )
    assert stream_select._playable_url(_native_cfg(), {"infoHash": "aaaa"}) == "http://cdn/p.mkv"


# --- audio language probing ------------------------------------------------


def test_audio_langs_of_trusts_preferred_tag(monkeypatch):
    def boom(url):
        raise AssertionError("ffprobe should be skipped for a preferred-tagged release")

    monkeypatch.setattr(stream_select.tracks, "probe_tracks", boom)
    s: Stream = {"url": "u", "title": "Movie.2024.1080p.ITA.ENG.x264-GRP"}
    assert stream_select._audio_langs_of(Config(torrentio_base="tb", audio_langs=["ita"]), s) == {
        "ita"
    }


def test_audio_langs_of_probes_untagged(monkeypatch):
    tr = stream_select.tracks
    monkeypatch.setattr(tr, "probe_tracks", lambda url: tr.Tracks(audio=[tr.Track(1, "es")]))
    s: Stream = {"url": "u", "title": "Some.Movie.2024.1080p.x264-GRP"}  # untagged
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    assert stream_select._audio_langs_of(cfg, s) == {"spa"}  # "es" → spa via the registry


def test_audio_langs_of_does_not_trust_multi(monkeypatch):
    # "Dual"/"MULTI" is ambiguous (may be Latino+Eng, no Italian); it must be probed,
    # not trusted as carrying a preferred track.
    tr = stream_select.tracks
    calls = []
    monkeypatch.setattr(
        tr,
        "probe_tracks",
        lambda url: calls.append(url) or tr.Tracks(audio=[tr.Track(1, "spa"), tr.Track(2, "eng")]),
    )
    s: Stream = {"url": "u", "title": "Dune.Part.Two.2024.Dual.1080p.x265-YG"}
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    assert stream_select._audio_langs_of(cfg, s) == {"spa", "eng"}  # real tracks, not the guess
    assert calls  # the probe actually ran


def test_audio_langs_of_reads_title_when_untagged(monkeypatch):
    # language=und but the track title names the language → recovered via track_lang.
    tr = stream_select.tracks
    monkeypatch.setattr(
        tr,
        "probe_tracks",
        lambda url: tr.Tracks(audio=[tr.Track(1, "und", title="Italian [TrueHD]")]),
    )
    s: Stream = {"url": "u", "title": "Movie.2024.1080p.x264"}
    assert stream_select._audio_langs_of(Config(torrentio_base="tb", audio_langs=["ita"]), s) == {
        "ita"
    }


def test_audio_langs_of_unverifiable_returns_none(monkeypatch):
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks", lambda url: stream_select.tracks.Tracks()
    )
    s: Stream = {"url": "u", "title": "Some.Movie.2024.1080p.x264-GRP"}
    assert (
        stream_select._audio_langs_of(Config(torrentio_base="tb", audio_langs=["ita"]), s) is None
    )


def test_audio_langs_of_no_preference_returns_none():
    s: Stream = {"url": "u", "title": "x"}
    assert stream_select._audio_langs_of(Config(torrentio_base="tb", audio_langs=[]), s) is None


def test_audio_langs_of_fallback_tag_probes(monkeypatch):
    # Regression: a release tagged only with a FALLBACK language (eng, primary ita) must be
    # probed, not trusted — trusting it hid the primary's absence (played eng, no warning).
    tr = stream_select.tracks
    calls = []
    monkeypatch.setattr(
        tr,
        "probe_tracks",
        lambda url: calls.append(url) or tr.Tracks(audio=[tr.Track(1, "eng"), tr.Track(2, "rus")]),
    )
    s: Stream = {"url": "u", "title": "Mr.Robot.S01.BDRemux.ENG.RUS.1080p"}
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    assert stream_select._audio_langs_of(cfg, s) == {"eng", "rus"}  # no ita → guard can act
    assert calls  # the probe actually ran


def test_audio_langs_of_trusts_primary_tag_intersection(monkeypatch):
    def boom(url):
        raise AssertionError("ffprobe should be skipped when the name tags the primary")

    monkeypatch.setattr(stream_select.tracks, "probe_tracks", boom)
    s: Stream = {"url": "u", "title": "Movie.2024.1080p.ITA.ENG.x264-GRP"}
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    assert stream_select._audio_langs_of(cfg, s) == {"ita", "eng"}  # tagged ∩ pref, not all pref


def test_audio_langs_of_fallback_tag_unverifiable_uses_name(monkeypatch):
    # Probe impossible on a fallback-tagged name → the name's info still beats None,
    # so the guard fires (reselect/safety-subs) instead of silently playing the fallback.
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks", lambda url: stream_select.tracks.Tracks()
    )
    s: Stream = {"url": "u", "title": "Movie.2024.1080p.ENG.x264-GRP"}
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    assert stream_select._audio_langs_of(cfg, s) == {"eng"}


# --- primary-language reselect (_reselect_for_primary) ----------------------


def _reselect_env(monkeypatch, candidates, *, real_langs, resolve_fail=()):
    """Wire _reselect_for_primary's collaborators: candidates list, identity resolve
    (None for urls in `resolve_fail`), and a name→langs map for stream_audio_langs."""
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: candidates)
    monkeypatch.setattr(
        stream_select,
        "_resolve_stream",
        lambda cfg, s: None if s.get("url") in resolve_fail else s,
    )
    monkeypatch.setattr(
        stream_select, "stream_audio_langs", lambda cfg, s: real_langs.get(s.get("url"))
    )


def test_reselect_for_primary_finds_deep_tagged(monkeypatch, capsys):
    # The ita-tagged release ranks far below cached eng ones: the probe budget must be
    # spent on name-tagged candidates, not burned on the next-best fallbacks.
    eng = [{"url": f"e{i}", "title": f"Show.S01.ENG.{i}.1080p"} for i in range(6)]
    ita: Stream = {"url": "i1", "title": "Show.S01.ITA.ENG.1080p"}
    resolved = []
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [*eng, ita])
    monkeypatch.setattr(
        stream_select, "_resolve_stream", lambda cfg, s: resolved.append(s["url"]) or s
    )
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"ita"}))
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    current: Stream = {"url": "cur", "title": "Show.S01.ENG.RUS.BDRemux"}
    got = stream_select._reselect_for_primary(cfg, [], current, _gopts(), "ita")
    assert got is ita
    assert resolved == ["i1"]  # the eng candidates were never resolved/probed
    assert "cerco una sorgente ita" in capsys.readouterr().err


def test_reselect_for_primary_skips_mistag(monkeypatch):
    a: Stream = {"url": "a", "title": "Show.ITA.fake.1080p"}
    b: Stream = {"url": "b", "title": "Show.ITA.real.1080p"}
    _reselect_env(
        monkeypatch, [a, b], real_langs={"a": frozenset({"eng"}), "b": frozenset({"ita"})}
    )
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    current: Stream = {"url": "cur", "title": "Show.ENG.1080p"}
    assert stream_select._reselect_for_primary(cfg, [], current, _gopts(), "ita") is b


def test_reselect_for_primary_unverifiable_tagged_accepted(monkeypatch):
    a: Stream = {"url": "a", "title": "Show.ITA.1080p"}
    _reselect_env(monkeypatch, [a], real_langs={})  # probe → None
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    current: Stream = {"url": "cur", "title": "Show.ENG.1080p"}
    assert stream_select._reselect_for_primary(cfg, [], current, _gopts(), "ita") is a


def test_reselect_for_primary_unverifiable_multi_skipped(monkeypatch):
    a: Stream = {"url": "a", "title": "Show.Dual.1080p"}  # "multi" maybe, probe fails
    _reselect_env(monkeypatch, [a], real_langs={})
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    current: Stream = {"url": "cur", "title": "Show.ENG.1080p"}
    assert stream_select._reselect_for_primary(cfg, [], current, _gopts(), "ita") is None


def test_reselect_for_primary_resolve_failure_continues(monkeypatch):
    a: Stream = {"url": "a", "title": "Show.ITA.dead.1080p"}
    b: Stream = {"url": "b", "title": "Show.ITA.alive.1080p"}
    _reselect_env(monkeypatch, [a, b], real_langs={"b": frozenset({"ita"})}, resolve_fail=("a",))
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    current: Stream = {"url": "cur", "title": "Show.ENG.1080p"}
    assert stream_select._reselect_for_primary(cfg, [], current, _gopts(), "ita") is b


def test_reselect_for_primary_respects_limit(monkeypatch):
    cands = [{"url": f"i{i}", "title": f"Show.ITA.{i}.1080p"} for i in range(6)]
    tried = []
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: cands)
    monkeypatch.setattr(
        stream_select, "_resolve_stream", lambda cfg, s: tried.append(s["url"]) or s
    )
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"eng"}))
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    current: Stream = {"url": "cur", "title": "Show.ENG.1080p"}
    assert stream_select._reselect_for_primary(cfg, [], current, _gopts(), "ita") is None
    assert len(tried) == 4  # limit, not the whole tagged list


# --- stream ranking + curation (_pick_stream) ------------------------------


def _ranked(n, *, reason=None):
    from nstream.quality import RankedStream, StreamInfo

    return [
        RankedStream({"url": f"u{i}", "name": f"S{i}"}, StreamInfo(resolution=1080), reason)
        for i in range(n)
    ]


def test_pick_stream_cap_and_show_all(monkeypatch):
    cfg = Config(torrentio_base="tb", max_streams=20)
    monkeypatch.setattr(
        stream_select.quality, "detect_caps", lambda *a, **k: stream_select.quality.Caps()
    )
    playable, excluded = _ranked(25), _ranked(2, reason="camrip (cam)")
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, excluded))
    calls = []
    headers = []

    def fzf(items, prompt, *, header=None):
        calls.append(items)  # both menus share the "stream> " prompt now
        headers.append(header)
        if len(calls) == 1:
            return items[-1][1]  # capped menu → the "↓ mostra tutti" sentinel
        return items[0][1]  # full menu → first stream

    monkeypatch.setattr(stream_select, "fzf", fzf)
    out = stream_select._pick_stream(cfg, [{"url": "x"}] * 27, auto=False)
    # Capped menu = 20 streams + 1 "show all" entry; full menu = 25 playable + 2 excluded.
    assert len(calls[0]) == 21
    assert "mostra tutti" in calls[0][-1][0]
    assert len(calls[1]) == 27
    assert out is playable[0].stream
    # Ranking notice lives in the fzf header (not only on stderr).
    assert headers[0] and "2 stream filtrati" in headers[0]
    assert "camrip" in headers[0]


def test_pick_stream_auto_picks_best(monkeypatch):
    cfg = Config(torrentio_base="tb")
    monkeypatch.setattr(
        stream_select.quality, "detect_caps", lambda *a, **k: stream_select.quality.Caps()
    )
    playable = _ranked(3)
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    assert stream_select._pick_stream(cfg, [{"url": "x"}], auto=True) is playable[0].stream


def test_pick_stream_cast_uses_cast_caps_and_audio(monkeypatch):
    """In cast mode rank against the Chromecast profile (not the laptop GPU) and pass
    cast_audio=True so the receiver-incompatible audio is filtered."""
    cfg = Config(torrentio_base="tb")

    def boom(*a, **k):
        raise AssertionError("detect_caps (GPU) must not be used when casting")

    monkeypatch.setattr(stream_select.quality, "detect_caps", boom)
    sentinel = stream_select.quality.Caps()
    monkeypatch.setattr(stream_select.quality, "cast_caps", lambda: sentinel)
    seen = {}
    playable = _ranked(2)

    def fake_rank(streams, caps, spec):
        seen["caps"] = caps
        seen["cast_audio"] = spec.cast_audio
        return (playable, [])

    monkeypatch.setattr(stream_select.quality, "rank_streams", fake_rank)
    assert (
        stream_select._pick_stream(cfg, [{"url": "x"}], auto=True, cast=True) is playable[0].stream
    )
    assert seen["caps"] is sentinel and seen["cast_audio"] is True


# --- cast stream selection (language switch) --------------------------------


_S_ITA: Stream = {
    "url": "http://ita",
    "name": "[RD+] Torrentio\n1080p",
    "title": "Film.2020.iTA.1080p.BluRay.DDP5.1.x264-GRP\n👤 20 💾 8.0 GB ⚙️ x",
}
_S_ENG_REMUX: Stream = {
    "url": "http://eng-remux",
    "name": "[RD+] Torrentio\n4k",
    "title": "Film.2020.ENG.2160p.UHD.BluRay.REMUX.TrueHD-GRP\n👤 30 💾 60.0 GB ⚙️ x",
}
_S_ENG_WEBDL: Stream = {
    "url": "http://eng-webdl",
    "name": "[RD+] Torrentio\n1080p",
    "title": "Film.2020.ENG.1080p.WEB-DL.DDP5.1.x264-GRP\n👤 10 💾 6.0 GB ⚙️ x",
}


def test_cast_languages_lists_compatible():
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    langs = stream_select.cast_languages(cfg, [_S_ITA, _S_ENG_REMUX, _S_ENG_WEBDL])
    assert langs == ("ita", "eng")  # preferred order; eng present via the WEB-DL


def test_cast_resolver_picks_compatible_release():
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    resolve = stream_select.cast_resolver(cfg, [_S_ITA, _S_ENG_REMUX, _S_ENG_WEBDL])
    # ITA → the ITA release; missing → None. For ENG both releases carry Dolby audio the
    # DMR can't decode (TrueHD / DDP), so both are castable via Tier-2 remux (default on).
    # The remux resolution cap (default 1080p) then prefers the 1080p WEB-DL over the 4K
    # TrueHD remux — a 4K remux would download tens of GB; the 1080p one is far cheaper.
    assert resolve("ita") == "http://ita"
    assert resolve("eng") == "http://eng-webdl"
    assert resolve("ger") is None


def test_cast_resolver_excludes_lossless_without_remux():
    # With Tier-2 remux disabled, the old behaviour holds: TrueHD is dropped as unplayable,
    # so ENG resolves to the (E-AC-3) WEB-DL instead of the 4K TrueHD remux.
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"], cast_remux=False)
    resolve = stream_select.cast_resolver(cfg, [_S_ITA, _S_ENG_REMUX, _S_ENG_WEBDL])
    assert resolve("eng") == "http://eng-webdl"


# --- cached-miss fallback (_ensure_playable) -------------------------------


def test_ensure_playable_passthrough_when_reachable(monkeypatch):
    monkeypatch.setattr(stream_select.api, "url_playable", lambda u, **k: True)
    chosen: Stream = {"url": "https://rd/u"}
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    assert stream_select._ensure_playable(cfg, [chosen], chosen, _gopts()) is chosen


def test_ensure_playable_local_skips_check(monkeypatch):
    monkeypatch.setattr(
        stream_select.api,
        "url_playable",
        lambda u, **k: pytest.fail("no reachability check in local mode"),
    )
    chosen: Stream = {"url": "http://127.0.0.1:8090/stream"}
    cfg = Config(torrentio_base="tb", playback_backend="local")
    assert stream_select._ensure_playable(cfg, [chosen], chosen, _gopts()) is chosen


def test_ensure_playable_hybrid_falls_back_to_p2p(monkeypatch):
    monkeypatch.setattr(stream_select.api, "url_playable", lambda u, **k: False)
    monkeypatch.setattr(
        stream_select.engine, "resolve", lambda cfg, s: "http://192.168.1.5:8090/stream"
    )
    chosen: Stream = {"url": "https://rd/dead", "infoHash": "ABC"}
    cfg = Config(torrentio_base="tb", playback_backend="auto")
    out = stream_select._ensure_playable(cfg, [chosen], chosen, _gopts())
    assert out["url"] == "http://192.168.1.5:8090/stream"  # fell back to local P2P


def test_ensure_playable_next_candidate_when_no_infohash(monkeypatch):
    dead: Stream = {"url": "https://rd/dead"}
    good: Stream = {"url": "https://rd/good"}
    monkeypatch.setattr(stream_select.api, "url_playable", lambda u, **k: u == "https://rd/good")
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [dead, good])
    monkeypatch.setattr(stream_select, "_resolve_stream", lambda cfg, s: s)
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    out = stream_select._ensure_playable(cfg, [dead, good], dead, _gopts())
    assert out is good  # skipped the dead cached link for the next reachable candidate


# --- pre-commit cached verification (_verify_cached_availability, ADR 0014) ---


def test_probe_url_memoizes(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(stream_select.api, "url_playable", lambda u, **k: calls.append(u) or True)
    assert stream_select._probe_url("http://x") is True
    assert stream_select._probe_url("http://x") is True
    assert calls == ["http://x"]  # probed once, second call served from the memo


def test_verify_cached_demotes_dead_keeps_live(monkeypatch):
    dead: Stream = {"url": "https://rd/dead", "name": "[RD+] Torrentio\n4k"}
    live: Stream = {"url": "https://rd/live", "name": "[RD+] Torrentio\n1080p"}
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [dead, live])
    monkeypatch.setattr(
        stream_select.api, "url_playable", lambda u, **k: u == "https://rd/live"
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    stream_select._verify_cached_availability(cfg, [dead, live], cast=False, title="")
    assert "[RD+]" not in dead["name"]  # dead cached link demoted to uncached-equivalent
    assert "[RD+]" in live["name"]  # live one keeps its marker
    assert not stream_select.quality.parse_stream(dead).cached  # flows through the rank pipeline


def test_verify_cached_bounded_to_cap(monkeypatch):
    streams: list[Stream] = [{"url": f"https://rd/{i}", "name": "[RD+] x\n1080p"} for i in range(8)]
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: streams)
    probed: list[str] = []
    monkeypatch.setattr(
        stream_select.api, "url_playable", lambda u, **k: probed.append(u) or True
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    stream_select._verify_cached_availability(cfg, streams, cast=False, title="")
    assert len(probed) == stream_select._VERIFY_CACHED_CAP  # only the top-N are probed


def test_verify_cached_skips_uncached(monkeypatch):
    uncached: Stream = {"url": "https://rd/u", "name": "Torrentio\n1080p"}  # no [XX+] marker
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [uncached])
    monkeypatch.setattr(
        stream_select.api, "url_playable",
        lambda u, **k: pytest.fail("an uncached candidate must not be probed"),
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    stream_select._verify_cached_availability(cfg, [uncached], cast=False, title="")


def test_verify_cached_noop_local_backend(monkeypatch):
    cached: Stream = {"url": "https://rd/u", "name": "[RD+] x\n1080p"}
    monkeypatch.setattr(
        stream_select.api, "url_playable",
        lambda u, **k: pytest.fail("no probe on the local backend"),
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", playback_backend="local")
    stream_select._verify_cached_availability(cfg, [cached], cast=False, title="")


# --- P2P privacy guard -----------------------------------------------------


def test_p2p_guard_blocks_without_vpn_when_required(monkeypatch, capsys):
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: False)
    cfg = Config(torrentio_base="tb", p2p_require_vpn=True)
    assert stream_select._p2p_guard(cfg) is False
    assert "bloccato" in capsys.readouterr().err


def test_p2p_guard_warns_without_vpn_but_proceeds(monkeypatch, capsys):
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: False)
    monkeypatch.setattr(stream_select, "_p2p_notice_once", lambda cfg: None)
    cfg = Config(torrentio_base="tb", p2p_require_vpn=False)
    assert stream_select._p2p_guard(cfg) is True
    assert "nessuna VPN" in capsys.readouterr().err


def test_p2p_guard_silent_with_vpn(monkeypatch, capsys):
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: True)
    monkeypatch.setattr(stream_select, "_p2p_notice_once", lambda cfg: None)
    assert stream_select._p2p_guard(Config(torrentio_base="tb")) is True
    assert "VPN" not in capsys.readouterr().err


# --- prepare_stream: the auto-play language guard --------------------------
#
# These isolate the guard by stubbing _ensure_playable to identity and the probe/
# reselect seams; pick_and_resolve runs for real (url-carrying streams resolve as-is).


def test_prepare_stream_reselects_on_wrong_audio(monkeypatch, capsys):
    foreign: Stream = {"url": "u1", "name": "x\n1080p"}
    chosen2: Stream = {"url": "u2", "name": "y\n1080p"}
    picks = iter([foreign, chosen2])
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: next(picks))
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda *a, **k: a[2])
    monkeypatch.setattr(stream_select, "_audio_langs_of", lambda cfg, ch: {"spa"})  # no ita/eng
    monkeypatch.setattr(stream_select, "_reselect_for_primary", lambda *a, **k: None)  # no better
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    v = stream_select.prepare_stream(
        cfg, [foreign, chosen2], _gopts(), auto=True, reselect_on_wrong_audio=True
    )
    assert v is not None and v.stream is chosen2  # reselected after the warning
    assert "nessuna traccia audio ita,eng" in capsys.readouterr().err


def test_prepare_stream_binge_warns_and_proceeds(monkeypatch, capsys):
    foreign: Stream = {"url": "u1", "name": "x\n1080p"}
    calls = []
    monkeypatch.setattr(
        stream_select, "_pick_stream", lambda *a, **k: (calls.append(1), foreign)[1]
    )
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda *a, **k: a[2])
    monkeypatch.setattr(stream_select, "_audio_langs_of", lambda cfg, ch: {"spa"})
    monkeypatch.setattr(stream_select, "_reselect_for_primary", lambda *a, **k: None)
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    v = stream_select.prepare_stream(
        cfg, [foreign], _gopts(), auto=True, reselect_on_wrong_audio=False
    )
    assert v is not None and v.stream is foreign and len(calls) == 1  # proceeded, no reselection
    assert "nessuna traccia audio" in capsys.readouterr().err


def test_prepare_stream_reselects_for_primary(monkeypatch):
    # Best pick lacks the primary language; a next-best candidate has it → switch to it.
    top: Stream = {"url": "u1", "name": "x\n1080p"}
    better: Stream = {"url": "u2", "name": "y\n1080p"}
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: top)
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda *a, **k: a[2])
    monkeypatch.setattr(stream_select, "_audio_langs_of", lambda cfg, ch: {"eng"})  # top has no ita
    monkeypatch.setattr(stream_select, "_reselect_for_primary", lambda *a, **k: better)
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    v = stream_select.prepare_stream(
        cfg, [top, better], _gopts(), auto=True, reselect_on_wrong_audio=True
    )
    assert v is not None and v.stream is better and v.safety_sub_lang is None


def test_prepare_stream_safety_subtitles(monkeypatch, capsys):
    # Audio only in a fallback language (eng), not the primary (ita), and no better source:
    # play it but turn on primary-language safety subtitles.
    chosen: Stream = {"url": "u1", "name": "x\n1080p"}
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: chosen)
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda *a, **k: a[2])
    monkeypatch.setattr(stream_select, "_audio_langs_of", lambda cfg, ch: {"eng"})  # fallback only
    monkeypatch.setattr(stream_select, "_reselect_for_primary", lambda *a, **k: None)
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    v = stream_select.prepare_stream(
        cfg, [chosen], _gopts(), auto=True, reselect_on_wrong_audio=True
    )
    assert v is not None and v.stream is chosen and v.safety_sub_lang == "ita"
    assert "sottotitoli ita attivati" in capsys.readouterr().err


def test_prepare_stream_quality_picker_interactive(monkeypatch):
    """When quality is undecided and interactive, the in-flow picker sets exact_resolution."""
    s4k: Stream = {
        "url": "u4k",
        "name": "[RD+] Torrentio\n4k",
        "title": "F.2025.2160p.WEB-DL.HEVC\n👤 9 💾 20 GB",
    }
    s1080: Stream = {
        "url": "u1080",
        "name": "[RD+] Torrentio\n1080p",
        "title": "F.2025.1080p.WEB-DL.HEVC\n👤 9 💾 8 GB",
    }
    seen: dict = {}

    def _pick(cfg, results, *, auto, cast=False, title="", exact_resolution=0):
        seen["exact"] = exact_resolution
        # Prefer 1080 when filtered; otherwise first.
        if exact_resolution == 1080:
            return s1080
        return s4k

    monkeypatch.setattr(stream_select, "pick_quality", lambda *a, **k: 1080)
    monkeypatch.setattr(stream_select, "_pick_stream", _pick)
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda *a, **k: a[2])
    monkeypatch.setattr(stream_select, "_audio_langs_of", lambda cfg, ch: {"ita"})
    cfg = Config(torrentio_base="tb", audio_langs=["ita"])
    v = stream_select.prepare_stream(
        cfg, [s4k, s1080], _gopts(quality=None), auto=True, reselect_on_wrong_audio=True
    )
    assert v is not None and v.quality == 1080 and v.stream is s1080
    assert seen["exact"] == 1080


def test_prepare_stream_quality_cli_skips_picker(monkeypatch):
    """CLI --quality 720: no picker, exact filter applied."""
    s720: Stream = {
        "url": "u720",
        "name": "Torrentio\n720p",
        "title": "F.2025.720p.WEB-DL\n👤 9 💾 3 GB",
    }
    seen: dict = {}

    def _pick(cfg, results, *, auto, cast=False, title="", exact_resolution=0):
        seen["exact"] = exact_resolution
        return s720

    monkeypatch.setattr(
        stream_select,
        "pick_quality",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("picker must not run")),
    )
    monkeypatch.setattr(stream_select, "_pick_stream", _pick)
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda *a, **k: a[2])
    monkeypatch.setattr(stream_select, "_audio_langs_of", lambda cfg, ch: {"ita"})
    cfg = Config(torrentio_base="tb", audio_langs=["ita"])
    v = stream_select.prepare_stream(
        cfg, [s720], _gopts(quality=720), auto=True, reselect_on_wrong_audio=True
    )
    assert v is not None and v.quality == 720 and seen["exact"] == 720


def test_prepare_stream_headless_no_picker(monkeypatch):
    """Headless (reselect=False, quality=None): Auto, no picker."""
    s: Stream = {"url": "u", "name": "x\n1080p", "title": "F.1080p\n👤 9"}
    monkeypatch.setattr(
        stream_select,
        "pick_quality",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no picker headless")),
    )
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: s)
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda *a, **k: a[2])
    monkeypatch.setattr(stream_select, "_audio_langs_of", lambda cfg, ch: {"ita"})
    cfg = Config(torrentio_base="tb", audio_langs=["ita"])
    v = stream_select.prepare_stream(
        cfg, [s], _gopts(quality=None), auto=True, reselect_on_wrong_audio=False
    )
    assert v is not None and v.quality == 0


def test_exact_resolution_mapping():
    assert stream_select.exact_resolution(None) == 0
    assert stream_select.exact_resolution(0) == 0
    assert stream_select.exact_resolution(1080) == 1080


def test_resolve_quality_cli_wins_over_picker(monkeypatch):
    monkeypatch.setattr(
        stream_select,
        "pick_quality",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("picker")),
    )
    cfg = Config(torrentio_base="tb")
    assert (
        stream_select.resolve_quality(cfg, [], _gopts(quality=720), cast=False, offer_picker=True)
        == 720
    )


def test_resolve_quality_esc_returns_none(monkeypatch):
    monkeypatch.setattr(stream_select, "pick_quality", lambda *a, **k: None)
    cfg = Config(torrentio_base="tb")
    assert (
        stream_select.resolve_quality(cfg, [], _gopts(quality=None), cast=False, offer_picker=True)
        is None
    )


# --- audio language discovery / forced dub ---------------------------------


def _rstream(url, langs):
    from nstream.quality import RankedStream, StreamInfo

    return RankedStream({"url": url, "name": "S"}, StreamInfo(languages=frozenset(langs)), "")


def test_audio_languages_preferred_first(monkeypatch):
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    playable = [_rstream("u1", {"eng"}), _rstream("u2", {"fre"}), _rstream("u3", {"ita", "eng"})]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    langs = stream_select.audio_languages(cfg, [], cast=False)
    assert langs[:2] == ("ita", "eng") and "fre" in langs  # preferred first, then rest


def test_pick_audio_stream_returns_first_with_lang(monkeypatch):
    cfg = Config(torrentio_base="tb")
    playable = [_rstream("u1", {"eng"}), _rstream("u2", {"ita"})]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    monkeypatch.setattr(stream_select, "_playable_url", lambda cfg, s: s.get("url"))
    assert stream_select.pick_audio_stream(cfg, [], "ita", cast=False)["url"] == "u2"


def test_pick_audio_stream_none_when_absent(monkeypatch):
    cfg = Config(torrentio_base="tb")
    playable = [_rstream("u1", {"eng"})]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    monkeypatch.setattr(stream_select, "_playable_url", lambda cfg, s: s.get("url"))
    assert stream_select.pick_audio_stream(cfg, [], "jpn", cast=False) is None


# --- track-accurate audio (ffprobe) ----------------------------------------


def test_stream_audio_langs_probes(monkeypatch):
    from nstream import tracks as tr

    monkeypatch.setattr(stream_select, "_playable_url", lambda cfg, s: "http://u")
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks",
        lambda url: tr.Tracks(audio=[tr.Track(1, "ita"), tr.Track(2, "eng")]),
    )  # fmt: skip
    langs = stream_select.stream_audio_langs(Config(torrentio_base="tb"), {"url": "u"})
    assert langs == frozenset({"ita", "eng"})


def test_stream_audio_langs_none_when_und(monkeypatch):
    from nstream import tracks as tr

    monkeypatch.setattr(stream_select, "_playable_url", lambda cfg, s: "http://u")
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks", lambda url: tr.Tracks(audio=[tr.Track(1, "und")])
    )
    # single und track → no identifiable language → None (unverifiable, don't block)
    assert stream_select.stream_audio_langs(Config(torrentio_base="tb"), {"url": "u"}) is None


def test_pick_audio_stream_verified_confirms(monkeypatch):
    cfg = Config(torrentio_base="tb")
    playable = [_rstream("u1", {"eng"}), _rstream("u2", {"ita"})]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    monkeypatch.setattr(stream_select, "_playable_url", lambda cfg, s: s.get("url"))
    # real tracks of u2 confirm ita
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"ita"}))
    stream, verified = stream_select.pick_audio_stream_verified(cfg, [], "ita", cast=False)
    assert stream["url"] == "u2" and verified is True


def test_pick_audio_stream_verified_rejects_mistag(monkeypatch):
    cfg = Config(torrentio_base="tb")
    playable = [_rstream("u1", {"ita"})]  # name tags ita…
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    monkeypatch.setattr(stream_select, "_playable_url", lambda cfg, s: s.get("url"))
    # …but the real tracks are eng only → mistag → no verified match
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"eng"}))
    stream, verified = stream_select.pick_audio_stream_verified(cfg, [], "ita", cast=False)
    assert stream is None and verified is False


# --- cast audio-language enforcement (vet_cast_audio) ----------------------

from nstream.tracks import Track  # noqa: E402


def _ccfg() -> Config:
    return Config(torrentio_base="t", primary_lang="ita", audio_langs=["ita", "eng"])


def _plan(audio, target="ita"):
    return stream_select._cast_plan_for({"url": "u"}, list(audio), target)


def test_cast_plan_direct_when_first_track_is_target_decodable():
    p = _plan([Track(1, "ita", "aac"), Track(2, "eng", "aac")])
    assert p.mode == "direct" and p.audio_index == 0 and p.real_lang == "ita" and p.verified


def test_cast_plan_remux_when_first_track_target_but_undecodable():
    # Italian is the first track but AC-3 → DMR can't decode → remux track 0 to AAC.
    p = _plan([Track(1, "ita", "ac3", 6), Track(2, "eng", "ac3", 6)])
    assert p.mode == "remux" and p.audio_index == 0 and p.real_lang == "ita"


def test_cast_plan_remux_selects_nondefault_target_track():
    # Default track is English; Italian is buried at index 3 → remux selects it.
    audio = [
        Track(1, "eng", "dts", 6),
        Track(2, "spa", "eac3", 6),
        Track(3, "fra", "aac"),
        Track(4, "ita", "eac3", 6),
    ]
    p = _plan(audio)
    assert p.mode == "remux" and p.audio_index == 3 and p.real_lang == "ita"


def test_cast_plan_remux_selects_nondefault_aac_track():
    # Italian present as a non-default AAC track → still remux (DMR plays track 0) but copy-able.
    p = _plan([Track(1, "eng", "aac"), Track(2, "ita", "aac")])
    assert p.mode == "remux" and p.audio_index == 1


def test_cast_plan_absent_when_no_target_track():
    p = _plan([Track(1, "eng", "aac"), Track(2, "fra", "aac")])
    assert p.mode == "absent" and p.real_lang == "eng"
    assert p.needs_remux is False  # decodable AAC fallback → direct cast is fine


def test_cast_plan_absent_dolby_track_still_needs_remux():
    # Root cause of silent audio: target (ita) absent, fallback dub's first track is E-AC3.
    # An `absent` plan must still flag a remux — a direct cast of Dolby goes out silent on the
    # Default Media Receiver. (Regression for I.S.S.: untagged-language Blu-Ray, eng E-AC3.)
    p = _plan([Track(1, "eng", "eac3", 6), Track(2, "fra", "ac3", 6)])
    assert p.mode == "absent" and p.real_lang == "eng"
    assert p.needs_remux is True


def test_cast_plan_unprobeable_is_direct_unverified():
    p = _plan([])
    assert p.mode == "direct" and p.verified is False


def test_cast_plan_no_preference_is_codec_only():
    assert _plan([Track(1, "eng", "eac3", 6)], target="").mode == "remux"
    assert _plan([Track(1, "eng", "aac")], target="").mode == "direct"


def test_vet_cast_audio_returns_plan_without_reselect_when_present(monkeypatch):
    monkeypatch.setattr(
        stream_select, "_cast_audio_tracks", lambda cfg, s: [Track(1, "ita", "ac3", 6)]
    )
    called = []
    monkeypatch.setattr(
        stream_select, "_reselect_cast_for_lang", lambda *a, **k: called.append(1) or None
    )
    plan = stream_select.vet_cast_audio(_ccfg(), [], {"url": "u"}, "ita")
    assert plan.mode == "remux" and called == []  # chosen had it → no reselect


def test_vet_cast_audio_reselects_when_chosen_lacks_target(monkeypatch):
    chosen: Stream = {"url": "eng-only"}
    alt: Stream = {"url": "ita-rel"}

    def tracks_of(cfg, s):
        return [Track(1, "eng", "aac")] if s is chosen else [Track(1, "ita", "aac")]

    monkeypatch.setattr(stream_select, "_cast_audio_tracks", tracks_of)
    monkeypatch.setattr(
        stream_select,
        "_cast_playable",
        lambda cfg, results: [_R(alt, frozenset({"ita"}))],
    )
    plan = stream_select.vet_cast_audio(_ccfg(), [chosen, alt], chosen, "ita")
    assert plan.mode == "direct" and plan.stream is alt and plan.real_lang == "ita"


def test_vet_cast_audio_absent_when_nobody_has_target(monkeypatch):
    monkeypatch.setattr(
        stream_select, "_cast_audio_tracks", lambda cfg, s: [Track(1, "eng", "aac")]
    )
    monkeypatch.setattr(stream_select, "_cast_playable", lambda cfg, results: [])
    plan = stream_select.vet_cast_audio(_ccfg(), [{"url": "u"}], {"url": "u"}, "ita")
    assert plan.mode == "absent"


class _R:
    """Minimal RankedStream stand-in (stream + name-tag languages) for reselect tests."""

    def __init__(self, stream, languages):
        self.stream = stream
        self.info = type("I", (), {"languages": languages})()


def test_pick_audio_verified_cap_checked_before_resolving(monkeypatch):
    """The probe cap must be enforced BEFORE `_playable_url`: resolving an over-cap
    candidate can cost a P2P buffering wait / a debrid add for a stream we discard."""
    cfg = Config(torrentio_base="tb")
    playable = [_rstream(f"u{i}", {"ita"}) for i in range(6)]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    resolved = []
    monkeypatch.setattr(
        stream_select, "_playable_url", lambda cfg, s: resolved.append(s["url"]) or s["url"]
    )
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"eng"}))
    stream, verified = stream_select.pick_audio_stream_verified(
        cfg, [], "ita", cast=False, probe_cap=2
    )
    assert stream is None and verified is False  # every probed name-match mistagged
    assert len(resolved) == 2  # over-cap candidates were never resolved


def test_cast_plan_all_und_tracks_benefit_of_the_doubt():
    """Every track `und`: mirror the local guard (unverifiable → cast it) instead of
    declaring the dub absent — the single mistagged track was often already right."""
    p = _plan([Track(1, "und", "aac")])
    assert p.mode == "direct" and p.verified is False and p.real_lang == "ita"
    p = _plan([Track(1, "und", "ac3")])
    assert p.mode == "remux" and p.verified is False  # codec still decides the tier


def test_reselect_prefers_tagged_unverified_over_wrong_language(monkeypatch):
    """The Dexter: Resurrection bug (2026-07-14): the top pick is a verified WRONG-language
    stream (4K eng/rus) and every ita-tagged release is non-cached → unprobeable (empty
    tracks → unverified direct). Reselect must return the name-tagged-ita release on benefit
    of the doubt, NOT give up and let the caller cast the confirmed-Russian pick. This makes
    the default cast consistent with the forced --audio-lang path (pick_audio_stream_verified),
    which already accepts an unverified name tag."""
    wrong: Stream = {"url": "rus-4k"}
    ita_tagged: Stream = {"url": "ita-webrip"}

    def tracks_of(cfg, s):
        # the wrong pick probes fine (rus first); the ita release is unprobeable
        return [Track(1, "rus", "eac3"), Track(2, "eng", "eac3")] if s is wrong else []

    monkeypatch.setattr(stream_select, "_cast_audio_tracks", tracks_of)
    monkeypatch.setattr(
        stream_select, "_cast_playable",
        lambda cfg, results: [
            _R(wrong, frozenset({"eng", "rus"})),
            _R(ita_tagged, frozenset({"eng", "ita"})),  # name explicitly claims ita
        ],
    )  # fmt: skip
    plan = stream_select.vet_cast_audio(_ccfg(), [wrong, ita_tagged], wrong, "ita")
    assert plan.mode == "direct" and plan.stream is ita_tagged
    assert plan.real_lang == "ita" and plan.verified is False  # honest: tagged, not confirmed


def test_reselect_multi_unprobeable_not_trusted_as_target(monkeypatch):
    """An unprobeable release tagged only `multi` (not the target language) is NOT a
    benefit-of-the-doubt match — `multi` doesn't promise ita specifically. With no better
    option, reselect returns None and the caller keeps the (absent) original plan."""
    wrong: Stream = {"url": "rus-4k"}
    multi: Stream = {"url": "multi-rel"}
    monkeypatch.setattr(
        stream_select, "_cast_audio_tracks",
        lambda cfg, s: [Track(1, "rus", "eac3")] if s is wrong else [],
    )  # fmt: skip
    monkeypatch.setattr(
        stream_select, "_cast_playable",
        lambda cfg, results: [_R(wrong, frozenset({"rus"})), _R(multi, frozenset({"multi"}))],
    )  # fmt: skip
    assert stream_select._reselect_cast_for_lang(_ccfg(), [], wrong, "ita") is None


def test_reselect_verified_direct_beats_tagged_guess(monkeypatch):
    """A verified ita release must win over an earlier unprobeable ita-tagged guess even if
    the guess is ranked higher — confidence beats a name tag."""
    wrong: Stream = {"url": "rus"}
    guess: Stream = {"url": "ita-guess"}  # higher-ranked, unprobeable
    real: Stream = {"url": "ita-real"}  # lower-ranked, verified ita

    def tracks_of(cfg, s):
        if s is wrong:
            return [Track(1, "rus", "aac")]
        return [] if s is guess else [Track(1, "ita", "aac")]

    monkeypatch.setattr(stream_select, "_cast_audio_tracks", tracks_of)
    monkeypatch.setattr(
        stream_select, "_cast_playable",
        lambda cfg, results: [
            _R(wrong, frozenset({"rus"})),
            _R(guess, frozenset({"ita"})),
            _R(real, frozenset({"ita"})),
        ],
    )  # fmt: skip
    plan = stream_select._reselect_cast_for_lang(_ccfg(), [], wrong, "ita")
    assert plan is not None and plan.stream is real and plan.verified is True
