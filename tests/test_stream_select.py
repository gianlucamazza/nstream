"""Unit tests for stream selection, resolution, and the auto-play vetting guards.

Patches are applied on the `stream_select` module (where the helpers are looked up).
Cast-path vetting lives in `tests/test_cast_vet.py`."""

from __future__ import annotations

import pytest

from nstream import availability, net, state, stream_select, tracks
from nstream.config import Config, PlayOpts
from nstream.types import Stream


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
    """The availability probe memo is process-lifetime — clear it between tests so a url's
    verdict from one test can't leak into another that stubs `probe_url` differently."""
    availability.clear_memo()
    tracks.clear_cache()
    yield
    availability.clear_memo()
    tracks.clear_cache()


@pytest.fixture(autouse=True)
def _isolated_dead_cache(monkeypatch, tmp_path):
    """The dead-source denylist (ADR 0025) is persistent: keep test verdicts out of the
    developer's real state dir, and out of each other's."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _probing(*live_urls: str, all_live: bool = False, seen: list[str] | None = None):
    """Fake `net.probe_url`: the listed urls answer `live`, every other one answers the
    benign `unknown` (unusable but never denylisted — the pre-ADR-0025 `False`)."""

    def _probe(url: str, **_kw) -> net.Probe:
        if seen is not None:
            seen.append(url)
        if all_live or url in live_urls:
            return net.Probe(net.LIVE)
        return net.Probe(net.UNKNOWN, reason="stub")

    return _probe


# --- release date message --------------------------------------------------


def test_future_release_parsing():
    assert stream_select._future_release("2999-12-18T00:00:00.000Z") is not None  # far future
    assert stream_select._future_release("2000-01-01T00:00:00.000Z") is None  # past
    assert stream_select._future_release(None) is None
    assert stream_select._future_release("not-a-date") is None


def test_no_streams_message_no_source(monkeypatch):
    cfg = Config(torrentio_base="tb", torrentio_enabled=False, addons=[])
    monkeypatch.setattr(stream_select.addons, "load_addon", lambda *a, **k: None)
    msg = stream_select.no_streams_message(cfg, "movie", "tt1", "X")
    assert "fonte stream" in msg
    assert stream_select.no_stream_source_error(cfg) == "no_stream_sources"


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
    monkeypatch.setattr(stream_select.engine, "resolve", lambda cfg, s: "http://127.0.0.1:8090/s")
    out = stream_select._resolve_stream(_native_cfg(), {"infoHash": "aaaa"})
    assert out is not None and out["url"] == "http://127.0.0.1:8090/s"


def test_playable_url_native(monkeypatch):
    monkeypatch.setattr(
        stream_select.debrid, "get_resolver", lambda cfg: _FakeResolver(resolved="http://cdn/p.mkv")
    )
    assert stream_select.playable_url(_native_cfg(), {"infoHash": "aaaa"}) == "http://cdn/p.mkv"


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
        stream_select.quality, "detect_caps", lambda *a, **k: stream_select.quality.HwCaps()
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
        stream_select.quality, "detect_caps", lambda *a, **k: stream_select.quality.HwCaps()
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
    sentinel = stream_select.quality.HwCaps()
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


# --- cached-miss fallback (_ensure_playable) -------------------------------


def test_ensure_playable_passthrough_when_reachable(monkeypatch):
    monkeypatch.setattr(availability.net, "probe_url", _probing(all_live=True))
    chosen: Stream = {"url": "https://rd/u"}
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    assert stream_select._ensure_playable(cfg, [chosen], chosen, _gopts()) is chosen


def test_ensure_playable_local_skips_check(monkeypatch):
    monkeypatch.setattr(
        availability.net,
        "probe_url",
        lambda u, **k: pytest.fail("no reachability check in local mode"),
    )
    chosen: Stream = {"url": "http://127.0.0.1:8090/stream"}
    cfg = Config(torrentio_base="tb", playback_backend="local")
    assert stream_select._ensure_playable(cfg, [chosen], chosen, _gopts()) is chosen


def test_ensure_playable_hybrid_falls_back_to_p2p(monkeypatch):
    monkeypatch.setattr(availability.net, "probe_url", _probing())
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
    monkeypatch.setattr(availability.net, "probe_url", _probing("https://rd/good"))
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [dead, good])
    monkeypatch.setattr(stream_select, "_resolve_stream", lambda cfg, s: s)
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    out = stream_select._ensure_playable(cfg, [dead, good], dead, _gopts())
    assert out is good  # skipped the dead cached link for the next reachable candidate


# --- pre-commit cached verification (_verify_availability, ADR 0014) ---


def test_probe_url_memoizes(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(availability.net, "probe_url", _probing(all_live=True, seen=calls))
    assert availability.probe_url("http://x") is True
    assert availability.probe_url("http://x") is True
    assert calls == ["http://x"]  # probed once, second call served from the memo


def test_verify_cached_demotes_dead_keeps_live(monkeypatch):
    dead: Stream = {"url": "https://rd/dead", "name": "[RD+] Torrentio\n4k"}
    live: Stream = {"url": "https://rd/live", "name": "[RD+] Torrentio\n1080p"}
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [dead, live])
    monkeypatch.setattr(availability.net, "probe_url", _probing("https://rd/live"))
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    stream_select._verify_availability(cfg, [dead, live], cast=False, title="")
    assert "[RD+]" not in dead["name"]  # dead cached link demoted to uncached-equivalent
    assert "[RD+]" in live["name"]  # live one keeps its marker
    assert not stream_select.quality.parse_stream(dead).cached  # flows through the rank pipeline


def test_verify_cached_bounded_to_cap(monkeypatch):
    streams: list[Stream] = [{"url": f"https://rd/{i}", "name": "[RD+] x\n1080p"} for i in range(8)]
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: streams)
    probed: list[str] = []
    monkeypatch.setattr(availability.net, "probe_url", _probing(all_live=True, seen=probed))
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    stream_select._verify_availability(cfg, streams, cast=False, title="")
    assert len(probed) == availability.VERIFY_CAP  # only the top-N are probed


def test_verify_probes_uncached_too(monkeypatch):
    """ADR 0025: the seeder count that gates uncached rows describes swarm health, which says
    nothing about whether the debrid still holds the file — so they get probed as well."""
    uncached: Stream = {"url": "https://rd/u", "name": "Torrentio\n1080p"}  # no [XX+] marker
    probed: list[str] = []
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [uncached])
    monkeypatch.setattr(availability.net, "probe_url", _probing(all_live=True, seen=probed))
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    stream_select._verify_availability(cfg, [uncached], cast=False, title="")
    assert probed == ["https://rd/u"]


def test_verify_cached_noop_local_backend(monkeypatch):
    cached: Stream = {"url": "https://rd/u", "name": "[RD+] x\n1080p"}
    monkeypatch.setattr(
        availability.net, "probe_url",
        lambda u, **k: pytest.fail("no probe on the local backend"),
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", playback_backend="local")
    stream_select._verify_availability(cfg, [cached], cast=False, title="")


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
    # `prepare_stream` decides the safety language but never fetches, so it may only state
    # the audio fact. The confirmation belongs to `subs.report_safety_subs`, which sees the
    # SubsPick; asserting it here is what let a promise ship ahead of its outcome.
    err = capsys.readouterr().err
    assert "audio non disponibile in ita (disponibili: eng)" in err
    assert "sottotitoli ita attivati" not in err


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


def test_prepare_stream_forced_audio_lang_raises_when_missing(monkeypatch):
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    eng: Stream = {"url": "http://e", "name": "X 1080p ENG", "title": "eng only"}
    monkeypatch.setattr(stream_select, "prune_dead", lambda cfg, r: (r, 0))
    monkeypatch.setattr(stream_select, "_mark_native_cached", lambda *a, **k: None)
    monkeypatch.setattr(stream_select, "resolve_quality", lambda *a, **k: 0)
    monkeypatch.setattr(stream_select, "audio_languages", lambda *a, **k: ("eng",))
    opts = PlayOpts(
        auto=True,
        cast=False,
        sub_mode=None,
        sub_lang=None,
        history=False,
        autoplay=False,
        audio_lang="ita",
        quality=0,
    )
    with pytest.raises(stream_select.AudioLangUnavailable) as ei:
        stream_select.prepare_stream(
            cfg, [eng], opts, auto=True, reselect_on_wrong_audio=False, title="X"
        )
    assert ei.value.lang == "ita"
    assert "eng" in ei.value.available


def test_prepare_stream_forced_audio_lang_picks_stream(monkeypatch):
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    ita: Stream = {"url": "http://i", "name": "X 1080p ITA", "title": "ita"}
    eng: Stream = {"url": "http://e", "name": "X 1080p ENG", "title": "eng"}
    monkeypatch.setattr(stream_select, "prune_dead", lambda cfg, r: (r, 0))
    monkeypatch.setattr(stream_select, "_mark_native_cached", lambda *a, **k: None)
    monkeypatch.setattr(stream_select, "resolve_quality", lambda *a, **k: 0)
    monkeypatch.setattr(stream_select, "audio_languages", lambda *a, **k: ("ita", "eng"))
    monkeypatch.setattr(
        stream_select,
        "pick_audio_stream_verified",
        lambda *a, **k: (ita, True),
    )
    opts = PlayOpts(
        auto=True,
        cast=False,
        sub_mode=None,
        sub_lang=None,
        history=False,
        autoplay=False,
        audio_lang="ita",
        quality=0,
    )
    v = stream_select.prepare_stream(
        cfg, [eng, ita], opts, auto=True, reselect_on_wrong_audio=False, title="X"
    )
    assert v is not None and v.stream is ita


def test_resolve_quality_uses_config_default_without_picker(monkeypatch):
    """cfg.default_quality skips the fzf picker when interactive."""
    monkeypatch.setattr(
        stream_select,
        "pick_quality",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("picker must not run")),
    )
    cfg = Config(torrentio_base="tb", default_quality=1080)
    opts = _gopts(quality=None)
    assert (
        stream_select.resolve_quality(cfg, [], opts, cast=False, title="X", offer_picker=True)
        == 1080
    )
    cfg2 = Config(torrentio_base="tb", default_quality=0)
    assert (
        stream_select.resolve_quality(cfg2, [], opts, cast=False, title="X", offer_picker=True) == 0
    )


def test_resolve_quality_default_applies_when_offer_picker_false(monkeypatch):
    """Headless / binge unattended honour cfg.default_quality (ADR 0021)."""
    monkeypatch.setattr(
        stream_select,
        "pick_quality",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no picker headless")),
    )
    cfg = Config(torrentio_base="tb", default_quality=1080)
    assert (
        stream_select.resolve_quality(cfg, [], _gopts(quality=None), cast=False, offer_picker=False)
        == 1080
    )


def test_resolve_quality_cli_beats_config_default(monkeypatch):
    monkeypatch.setattr(
        stream_select,
        "pick_quality",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no picker")),
    )
    cfg = Config(torrentio_base="tb", default_quality=1080)
    opts = _gopts(quality=720)
    assert stream_select.resolve_quality(cfg, [], opts, cast=False, offer_picker=True) == 720


def test_prepare_stream_raises_quality_unavailable(monkeypatch):
    """Auto pick with hard tier and no matching stream raises QualityUnavailable."""
    s720: Stream = {
        "url": "u720",
        "name": "Torrentio\n720p",
        "title": "F.2025.720p.WEB-DL\n👤 9 💾 3 GB",
    }
    monkeypatch.setattr(stream_select, "prune_dead", lambda cfg, r: (r, 0))
    monkeypatch.setattr(stream_select, "_mark_native_cached", lambda *a, **k: None)
    monkeypatch.setattr(stream_select, "resolve_quality", lambda *a, **k: 1080)
    monkeypatch.setattr(stream_select, "pick_and_resolve", lambda *a, **k: None)
    monkeypatch.setattr(stream_select, "available_resolutions", lambda *a, **k: [720])
    cfg = Config(torrentio_base="tb", audio_langs=["ita"])
    with pytest.raises(stream_select.QualityUnavailable) as ei:
        stream_select.prepare_stream(
            cfg, [s720], _gopts(quality=1080), auto=True, reselect_on_wrong_audio=False
        )
    assert ei.value.quality == 1080
    assert 720 in ei.value.available


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


def test_pick_audio_verified_cap_checked_before_resolving(monkeypatch):
    """The probe cap must be enforced BEFORE `playable_url`: resolving an over-cap
    candidate can cost a P2P buffering wait / a debrid add for a stream we discard."""
    cfg = Config(torrentio_base="tb")
    playable = [_rstream(f"u{i}", {"ita"}) for i in range(6)]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    resolved = []
    monkeypatch.setattr(
        stream_select, "playable_url", lambda cfg, s: resolved.append(s["url"]) or s["url"]
    )
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"eng"}))
    stream, verified = stream_select.pick_audio_stream_verified(
        cfg, [], "ita", cast=False, probe_cap=2
    )
    assert stream is None and verified is False  # every probed name-match mistagged
    assert len(resolved) == 2  # over-cap candidates were never resolved


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
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    selected = stream_select.pick_audio_stream(cfg, [], "ita", cast=False)
    assert selected is not None and selected["url"] == "u2"


def test_pick_audio_stream_none_when_absent(monkeypatch):
    cfg = Config(torrentio_base="tb")
    playable = [_rstream("u1", {"eng"})]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    assert stream_select.pick_audio_stream(cfg, [], "jpn", cast=False) is None


# --- track-accurate audio (ffprobe) ----------------------------------------


def test_stream_audio_langs_probes(monkeypatch):
    from nstream import tracks as tr

    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: "http://u")
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks",
        lambda url: tr.Tracks(audio=[tr.Track(1, "ita"), tr.Track(2, "eng")]),
    )  # fmt: skip
    langs = stream_select.stream_audio_langs(Config(torrentio_base="tb"), {"url": "u"})
    assert langs == frozenset({"ita", "eng"})


def test_stream_audio_langs_none_when_und(monkeypatch):
    from nstream import tracks as tr

    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: "http://u")
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
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    # real tracks of u2 confirm ita
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"ita"}))
    stream, verified = stream_select.pick_audio_stream_verified(cfg, [], "ita", cast=False)
    assert stream is not None
    assert stream["url"] == "u2" and verified is True


def test_pick_audio_stream_verified_rejects_mistag(monkeypatch):
    cfg = Config(torrentio_base="tb")
    playable = [_rstream("u1", {"ita"})]  # name tags ita…
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    # …but the real tracks are eng only → mistag → no verified match
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"eng"}))
    stream, verified = stream_select.pick_audio_stream_verified(cfg, [], "ita", cast=False)
    assert stream is None and verified is False


# --- content duration vetting (ADR 0028) -----------------------------------


def _durations(monkeypatch, by_url: dict[str, float]):
    """Stub the duration probe: url → real seconds (missing url = unreadable)."""
    monkeypatch.setattr(
        stream_select.availability.tracks,
        "probe_tracks",
        lambda url, **kw: tracks.Tracks(duration=by_url.get(url, 0.0)),
    )


def test_pick_audio_verified_skips_short_file(monkeypatch):
    """The incident: a P2P placeholder of 30s vs a ~55 min episode must be skipped, not
    accepted on benefit of the doubt because its single track is `und`."""
    cfg = Config(torrentio_base="tb")
    playable = [_rstream("fake", {"eng"}), _rstream("real", {"eng"})]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: None)  # und tracks
    _durations(monkeypatch, {"fake": 30.0, "real": 3180.0})
    stream, verified = stream_select.pick_audio_stream_verified(
        cfg, [], "eng", cast=False, expected_runtime_s=3300.0
    )
    assert stream is not None
    assert stream["url"] == "real" and verified is False


def test_pick_audio_verified_raises_when_all_short(monkeypatch):
    """All name-matches truncated → ContentTooShort, never `(None, False)`: the caller
    would map that to `audio_lang_unavailable`, a false diagnosis."""
    cfg = Config(torrentio_base="tb")
    playable = [_rstream("f1", {"eng"}), _rstream("f2", {"eng"})]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"eng"}))
    _durations(monkeypatch, {"f1": 30.0, "f2": 12.0})
    with pytest.raises(stream_select.ContentTooShort) as e:
        stream_select.pick_audio_stream_verified(
            cfg, [], "eng", cast=False, expected_runtime_s=3300.0
        )
    assert e.value.count == 2 and e.value.verdict.duration == 12.0


def test_pick_audio_verified_no_expected_keeps_current_behaviour(monkeypatch):
    cfg = Config(torrentio_base="tb")
    playable = [_rstream("u1", {"ita"})]
    monkeypatch.setattr(stream_select.quality, "detect_caps", lambda: object())
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: (playable, []))
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    monkeypatch.setattr(stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"ita"}))
    monkeypatch.setattr(
        stream_select.availability.tracks,
        "probe_tracks",
        lambda *a, **k: pytest.fail("must not probe when the runtime is unknown"),
    )
    stream, verified = stream_select.pick_audio_stream_verified(cfg, [], "ita", cast=False)
    assert stream is not None
    assert stream["url"] == "u1" and verified is True


def test_vet_duration_reselects_next_candidate(monkeypatch):
    cfg = Config(torrentio_base="tb")
    fake: Stream = {"url": "fake"}
    real: Stream = {"url": "real"}
    results: list[Stream] = [fake, real]
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [fake, real])
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    _durations(monkeypatch, {"fake": 30.0, "real": 3180.0})
    picked = stream_select.vet_duration(cfg, results, fake, expected_s=3300.0, cast=False)
    assert picked is real
    assert results == [real]  # dropped in place: no later reselect can land back on it


def test_vet_duration_respects_probe_cap(monkeypatch):
    cfg = Config(torrentio_base="tb")
    streams: list[Stream] = [{"url": f"f{i}"} for i in range(6)]
    results = list(streams)
    resolved = []
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: streams)
    monkeypatch.setattr(
        stream_select, "playable_url", lambda cfg, s: resolved.append(s["url"]) or s["url"]
    )
    _durations(monkeypatch, {s["url"]: 30.0 for s in streams})
    with pytest.raises(stream_select.ContentTooShort):
        stream_select.vet_duration(cfg, results, streams[0], expected_s=3300.0, cast=False)
    assert len(resolved) == 3  # chosen + probe_cap (2) alternatives, then honest failure


def test_vet_duration_unverifiable_candidate_accepted(monkeypatch):
    cfg = Config(torrentio_base="tb")
    fake: Stream = {"url": "fake"}
    unknown: Stream = {"url": "unknown"}
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [fake, unknown])
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    _durations(monkeypatch, {"fake": 30.0})  # "unknown" probes to 0 → benefit of the doubt
    assert stream_select.vet_duration(
        cfg, [fake, unknown], fake, expected_s=3300.0, cast=False
    ) is unknown  # fmt: skip


def test_prepare_stream_vets_duration_on_local_backend(monkeypatch):
    """The local backend is exactly where `_verify_availability`/`_ensure_playable` are
    no-ops — the duration guard must still act there (it's the incident's backend)."""
    fake: Stream = {"url": "fake", "name": "x\n720p"}
    real: Stream = {"url": "real", "name": "y\n720p"}
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: fake)
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [fake, real])
    monkeypatch.setattr(stream_select, "_resolve_stream", lambda cfg, s: s)
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    monkeypatch.setattr(stream_select, "_audio_langs_of", lambda cfg, ch: None)
    _durations(monkeypatch, {"fake": 30.0, "real": 3180.0})
    cfg = Config(torrentio_base="tb", playback_backend="local")
    v = stream_select.prepare_stream(
        cfg, [fake, real], _gopts(), auto=True, reselect_on_wrong_audio=False,
        expected_runtime_s=3300.0,
    )  # fmt: skip
    assert v is not None and v.stream is real


def test_prepare_stream_duration_guard_before_audio_guard(monkeypatch):
    """The language reselect must start from a duration-vetted pick, not from the fake."""
    fake: Stream = {"url": "fake", "name": "x\n720p"}
    real: Stream = {"url": "real", "name": "y\n720p"}
    seen = []
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: fake)
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: [fake, real])
    monkeypatch.setattr(stream_select, "_resolve_stream", lambda cfg, s: s)
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    monkeypatch.setattr(
        stream_select, "_audio_langs_of", lambda cfg, ch: seen.append(ch["url"]) or {"ita"}
    )
    _durations(monkeypatch, {"fake": 30.0, "real": 3180.0})
    cfg = Config(torrentio_base="tb", audio_langs=["ita"])
    stream_select.prepare_stream(
        cfg, [fake, real], _gopts(), auto=True, reselect_on_wrong_audio=False,
        expected_runtime_s=3300.0,
    )  # fmt: skip
    assert seen == ["real"]


def test_prepare_stream_no_duration_check_on_manual_pick(monkeypatch):
    """A manual pick stays the user's explicit choice, like every other auto-only guard."""
    fake: Stream = {"url": "fake", "name": "x\n720p"}
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: fake)
    monkeypatch.setattr(stream_select, "_resolve_stream", lambda cfg, s: s)
    monkeypatch.setattr(
        stream_select.availability.tracks,
        "probe_tracks",
        lambda *a, **k: pytest.fail("no probing on a manual pick"),
    )
    cfg = Config(torrentio_base="tb")
    v = stream_select.prepare_stream(
        cfg, [fake], _gopts(), auto=False, reselect_on_wrong_audio=False,
        expected_runtime_s=3300.0,
    )  # fmt: skip
    assert v is not None and v.stream is fake


def test_vet_duration_passes_plausible_chosen_through(monkeypatch):
    cfg = Config(torrentio_base="tb")
    good: Stream = {"url": "good"}
    monkeypatch.setattr(
        stream_select, "_auto_candidates", lambda *a, **k: pytest.fail("no reselect needed")
    )
    monkeypatch.setattr(stream_select, "playable_url", lambda cfg, s: s.get("url"))
    _durations(monkeypatch, {"good": 3180.0})
    assert stream_select.vet_duration(cfg, [good], good, expected_s=3300.0, cast=False) is good


# --- dead-source classification & denylist (ADR 0025) ----------------------


def test_source_key_prefers_infohash():
    assert availability.source_key({"infoHash": "ABC123"}) == "abc123"
    assert availability.source_key({"behaviorHints": {"filename": "M.mkv"}}) == "file:M.mkv"
    # ADR 0038: a shared display name is never a key.
    assert availability.source_key({"name": "[RD+] Torrentio\n1080p"}) == ""
    assert availability.source_key({}) == ""


def test_expected_bytes_from_announced_size():
    assert availability.expected_bytes({"name": "x\n💾 7.16 GB"}) == int(7.16 * 1024**3)
    assert availability.expected_bytes({"name": "x\n1080p"}) == 0


def test_probe_marks_gone_source_dead(monkeypatch, capsys):
    monkeypatch.setattr(
        availability.net,
        "probe_url",
        lambda u, **k: net.Probe(net.GONE, status=404, reason="HTTP 404"),
    )
    stream: Stream = {"url": "https://rd/x", "infoHash": "DEAD01", "name": "[RD+] x\n1080p"}
    assert availability.probe_stream(stream).dead is True
    assert state.is_dead(availability.source_key(stream)) is True  # remembered across runs
    assert "non più disponibile" in capsys.readouterr().err


def test_probe_does_not_denylist_transient_failure(monkeypatch):
    monkeypatch.setattr(availability.net, "probe_url", lambda u, **k: net.Probe(net.UNKNOWN))
    stream: Stream = {"url": "https://rd/x", "infoHash": "FLAKY1"}
    assert availability.probe_stream(stream).usable is False
    assert state.is_dead(availability.source_key(stream)) is False  # a hiccup never bans


def test_prune_dead_filters_known_removed():
    alive: Stream = {"infoHash": "ZZZ", "url": "https://rd/ok"}
    dead: Stream = {"infoHash": "ABC123", "url": "https://rd/gone"}
    state.mark_dead(availability.source_key(dead), "HTTP 404")
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    kept, dropped = availability.prune_dead(cfg, [dead, alive])
    assert kept == [alive] and dropped == 1


def test_prune_dead_noop_on_local_backend():
    state.mark_dead("abc123", "HTTP 404")
    dead: Stream = {"infoHash": "ABC123"}
    cfg = Config(torrentio_base="tb", playback_backend="local")
    assert availability.prune_dead(cfg, [dead]) == ([dead], 0)


def test_verify_drops_gone_and_denylists(monkeypatch):
    gone: Stream = {"url": "https://rd/gone", "infoHash": "G1", "name": "[RD+] x\n4k"}
    live: Stream = {"url": "https://rd/live", "infoHash": "L1", "name": "[RD+] x\n1080p"}
    results = [gone, live]
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: list(results))
    monkeypatch.setattr(
        availability.net,
        "probe_url",
        lambda u, **k: (
            net.Probe(net.LIVE) if u == "https://rd/live" else net.Probe(net.GONE, status=404)
        ),
    )
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    out = stream_select._verify_availability(cfg, results, cast=False, title="")
    assert out == [live] and results == [live]  # dropped, not merely demoted
    assert state.is_dead(availability.source_key(gone))
    assert not state.is_dead(availability.source_key(live))


def test_prepare_stream_raises_when_all_sources_removed(monkeypatch):
    gone: Stream = {"url": "https://rd/gone", "infoHash": "G9", "name": "[RD+] x\n1080p"}
    results = [gone]
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: list(results))
    monkeypatch.setattr(
        availability.net, "probe_url", lambda u, **k: net.Probe(net.GONE, status=410)
    )
    monkeypatch.setattr(
        stream_select, "_pick_stream", lambda *a, **k: pytest.fail("nothing left to pick")
    )
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    with pytest.raises(stream_select.NoPlayableStream) as e:
        stream_select.prepare_stream(
            cfg, results, _gopts(), auto=True, reselect_on_wrong_audio=False, title="T"
        )
    assert "--forget-dead" in e.value.reason and results == []


# --- why nothing is playable (NoPlayableStream / unresolvable_reason) ------


def test_unresolvable_reason_blames_the_p2p_gate(monkeypatch):
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: False)
    cfg = Config(torrentio_base="tb", p2p_require_vpn=True)
    reason = stream_select.unresolvable_reason(cfg, [{"infoHash": "A"}, {"infoHash": "B"}])
    assert reason and "torrent" in reason and "VPN" in reason


def test_unresolvable_reason_prefers_the_candidate_that_failed(monkeypatch):
    """Real case: two junk `url` rows the ranking put last still leave the auto-pick a pure
    torrent — the set's shape would say nothing, the failed candidate says everything."""
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: False)
    cfg = Config(torrentio_base="tb", p2p_require_vpn=True)
    results: list[Stream] = [{"url": "https://x/dead"}, {"infoHash": "A"}]
    assert stream_select.unresolvable_reason(cfg, results) is None
    reason = stream_select.unresolvable_reason(cfg, results, results[1])
    assert reason and "VPN" in reason


def test_unresolvable_reason_silent_when_a_direct_link_exists(monkeypatch):
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: False)
    cfg = Config(torrentio_base="tb", p2p_require_vpn=True)
    assert stream_select.unresolvable_reason(cfg, [{"url": "https://rd/x"}]) is None
    # A url already proven unresolvable doesn't count as a link.
    assert (
        stream_select.unresolvable_reason(
            cfg, [{"url": "https://rd/x", "infoHash": "A", "unresolvable": True}]
        )
        is not None
    )


def test_unresolvable_reason_reports_missing_debrid_links(monkeypatch):
    """No torrents to fall back on either: the set carries no servable link at all."""
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: True)
    cfg = Config(torrentio_base="tb")
    reason = stream_select.unresolvable_reason(cfg, [{"name": "x"}])
    assert reason and "debrid" in reason


def test_prepare_stream_raises_with_the_reason_when_p2p_is_blocked(monkeypatch):
    """The case that used to drop back to the menu in silence (ADR 0032 gate + no debrid)."""
    only: Stream = {"infoHash": "A", "name": "x\n1080p"}
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: only)
    monkeypatch.setattr(stream_select, "_verify_availability", lambda cfg, r, **k: r)
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: False)

    def _blocked(cfg, stream):
        raise stream_select.engine.P2PBlocked("streaming P2P bloccato: nessuna VPN")

    monkeypatch.setattr(stream_select.engine, "resolve", _blocked)
    cfg = Config(torrentio_base="tb", p2p_require_vpn=True)
    with pytest.raises(stream_select.NoPlayableStream) as e:
        stream_select.prepare_stream(
            cfg, [only], _gopts(), auto=True, reselect_on_wrong_audio=False, title="T"
        )
    assert "VPN" in e.value.reason


def test_prepare_stream_still_returns_none_on_esc(monkeypatch):
    """ESC stays None: the exception means exhaustion, never a user backing out."""
    s: Stream = {"url": "https://rd/x"}
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: None)
    monkeypatch.setattr(stream_select, "_verify_availability", lambda cfg, r, **k: r)
    cfg = Config(torrentio_base="tb")
    out = stream_select.prepare_stream(
        cfg, [s], _gopts(), auto=False, reselect_on_wrong_audio=False, title="T"
    )
    assert out is None


def test_pick_stream_raises_on_empty_ranking(monkeypatch):
    """An empty menu never opens fzf — without the raise it is indistinguishable from ESC."""
    monkeypatch.setattr(stream_select.quality, "rank_streams", lambda *a, **k: ([], []))
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: False)
    cfg = Config(torrentio_base="tb", p2p_require_vpn=True)
    with pytest.raises(stream_select.NoPlayableStream):
        stream_select._pick_stream(cfg, [{"infoHash": "A"}], auto=False)
    with pytest.raises(stream_select.NoPlayableStream):
        stream_select._pick_stream(cfg, [{"infoHash": "A"}], auto=True)


def test_prepare_stream_prunes_denylisted_before_ranking(monkeypatch):
    dead: Stream = {"infoHash": "OLD1", "url": "https://rd/old"}
    state.mark_dead(availability.source_key(dead), "HTTP 404")
    live: Stream = {"infoHash": "NEW1", "url": "https://rd/new"}
    results = [dead, live]
    seen: list[list[Stream]] = []
    monkeypatch.setattr(stream_select, "_verify_availability", lambda cfg, r, **k: r)
    monkeypatch.setattr(
        stream_select, "_pick_stream", lambda cfg, r, **k: seen.append(list(r)) or r[0]
    )
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    out = stream_select.prepare_stream(
        cfg, results, _gopts(), auto=True, reselect_on_wrong_audio=False, title="T"
    )
    assert out is not None and out.stream is live
    assert seen == [[live]]  # the removed source never reached the ranking


def test_incomplete_source_is_skipped_but_never_denylisted(monkeypatch):
    """Field case 2026-07-30: `[RD download]` served 2 MiB of an announced 7.16 GB while
    Real-Debrid was still fetching it. Skipping it this run is right; remembering it for 30
    days would lock out a title that is about to work."""
    monkeypatch.setattr(
        availability.net,
        "probe_url",
        lambda url, **_kw: net.Probe(net.UNKNOWN, status=206, reason="file incompleto"),
    )
    downloading: Stream = {
        "url": "https://rd/dl",
        "infoHash": "DL1",
        "name": "[RD download] Torrentio\n1080p 💾 7.16 GB",
    }
    probe = availability.probe_stream(downloading)
    assert not probe.usable and not probe.dead
    assert not state.is_dead("dl1")


def test_verify_drops_unusable_for_this_run_without_remembering(monkeypatch):
    """The two consequences are distinct: dropped now (any unusable verdict) vs remembered
    (only a proven-gone one)."""
    flaky: Stream = {"url": "https://rd/flaky", "infoHash": "F1", "name": "[RD+] x\n1080p"}
    live: Stream = {"url": "https://rd/live", "infoHash": "L1", "name": "[RD+] x\n1080p"}
    results = [flaky, live]
    monkeypatch.setattr(stream_select, "_auto_candidates", lambda *a, **k: list(results))
    monkeypatch.setattr(
        availability.net,
        "probe_url",
        lambda u, **k: net.Probe(net.LIVE) if u.endswith("live") else net.Probe(net.UNKNOWN),
    )
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    out = stream_select._verify_availability(cfg, results, cast=False, title="")
    assert out == [live]
    assert not state.is_dead("f1")  # dropped for this run only — nothing proven


def test_playable_url_memoizes_the_failure(monkeypatch):
    """A dead swarm costs `engine._wait_buffer`'s full timeout. Without a negative memo the
    cast vetting pays it again on every gate that probes the same candidate — the positive
    half (caching `url`) has always been there (ADR 0031 appendix)."""
    cfg = Config(torrentio_base="tb")
    stream: Stream = {"infoHash": "deadbeef"}
    calls = []
    monkeypatch.setattr(stream_select, "_native_resolve", lambda c, s: None)
    monkeypatch.setattr(
        stream_select.engine, "resolve",
        lambda c, s: calls.append(s["infoHash"]) or _raise_unavailable(),
    )  # fmt: skip
    assert stream_select.playable_url(cfg, stream) is None
    assert stream_select.playable_url(cfg, stream) is None
    assert stream_select.playable_url(cfg, stream) is None
    assert calls == ["deadbeef"]
    assert stream["unresolvable"] is True


def _raise_unavailable():
    raise stream_select.engine.EngineUnavailable("nessun peer")


def test_playable_url_is_covered_by_the_privacy_gate(monkeypatch, capsys):
    """The live 2026-08-08 exposure. `playable_url` is the resolver the WHOLE cast vetting
    runs on, and it was the one path `_p2p_guard` never protected: with p2p_require_vpn set
    and no VPN up, casting joined the swarm and exposed the real IP (ADR 0032)."""
    cfg = Config(torrentio_base="tb", p2p_require_vpn=True, p2p_ack=True)
    monkeypatch.setattr(stream_select.engine, "_p2p_gate_said", False)
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: False)
    monkeypatch.setattr(stream_select, "_native_resolve", lambda cfg, s: None)
    monkeypatch.setattr(
        stream_select.engine, "ensure_running",
        lambda cfg: pytest.fail("cast path joined the swarm despite p2p_require_vpn"),
    )  # fmt: skip
    stream: Stream = {"infoHash": "deadbeef"}
    assert stream_select.playable_url(cfg, stream) is None
    assert "bloccato" in capsys.readouterr().err
    # Memoised like any other failed resolve: the vetting probes many candidates and must not
    # re-enter the gate (nor reprint) for each one.
    assert stream["unresolvable"] is True


def test_native_debrid_resolve_is_not_gated(monkeypatch):
    """A debrid fetch is an HTTP GET from a provider: it joins no swarm and exposes nothing,
    so the gate must not degrade the one backend that is private by construction."""
    cfg = Config(torrentio_base="tb", p2p_require_vpn=True)
    monkeypatch.setattr(stream_select.engine, "vpn_active", lambda: False)
    monkeypatch.setattr(stream_select, "_native_resolve", lambda cfg, s: "http://rd/x.mkv")
    monkeypatch.setattr(
        stream_select.engine, "resolve", lambda cfg, s: pytest.fail("engine used for a debrid url")
    )
    assert stream_select.playable_url(cfg, {"infoHash": "aaaa"}) == "http://rd/x.mkv"


def test_prepare_candidates_matches_play_defaults():
    # ADR 0037: explain/probe share the play path's candidate pass — dead sources pruned and
    # cfg.default_quality applied — so --explain can't describe a pick that won't play.
    dead: Stream = {"infoHash": "D1", "url": "https://rd/d"}
    live: Stream = {"infoHash": "L1", "url": "https://rd/l"}
    state.mark_dead(availability.source_key(dead), "HTTP 404")
    cfg = Config(torrentio_base="tb", playback_backend="debrid", default_quality=1080)
    results = [dead, live]
    assert stream_select.prepare_candidates(cfg, results) == 1080
    assert results == [live]
    assert stream_select.prepare_candidates(cfg, [live], 720) == 720  # explicit choice wins


def test_manual_pick_that_wont_resolve_raises_not_esc(monkeypatch):
    # ADR 0033: None means ESC. A manual pick that can't be served used to return None too,
    # so the TUI showed nothing at all.
    bare: Stream = {"name": "x\n1080p", "title": "x"}  # neither url nor infoHash
    monkeypatch.setattr(stream_select, "_pick_stream", lambda *a, **k: bare)
    with pytest.raises(stream_select.NoPlayableStream):
        stream_select.pick_and_resolve(Config(torrentio_base="tb"), [bare], auto=False, cast=False)


def test_unfiltered_exact_quality_exhaustion_raises():
    s720: Stream = {"name": "x\n720p", "title": "Movie.720p", "url": "u"}
    cfg = Config(torrentio_base="tb", hw_filter=False)
    with pytest.raises(stream_select.QualityUnavailable) as e:
        stream_select._pick_stream(cfg, [s720], auto=True, cast=False, exact_resolution=1080)
    assert e.value.available == [720]
