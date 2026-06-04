"""Unit tests for stream selection, resolution, and the auto-play vetting guards.

Patches are applied on the `stream_select` module (where the helpers are looked up),
not on `cli` — the orchestrator only calls `prepare_stream`/`pick_and_resolve`."""

from __future__ import annotations

import pytest

from nstream import stream_select
from nstream.config import Config, PlayOpts, Stream


def _gopts(*, cast: bool = False) -> PlayOpts:
    return PlayOpts(
        auto=True, cast=cast, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )


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

    def fzf(items, prompt, *, header=None):
        calls.append(items)  # both menus share the "stream> " prompt now
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
    # ITA → the ITA release; ENG → the WEB-DL, never the TrueHD remux; missing → None
    assert resolve("ita") == "http://ita"
    assert resolve("eng") == "http://eng-webdl"
    assert resolve("ger") is None


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
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda cfg, r, c, o: c)
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
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda cfg, r, c, o: c)
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
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda cfg, r, c, o: c)
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
    monkeypatch.setattr(stream_select, "_ensure_playable", lambda cfg, r, c, o: c)
    monkeypatch.setattr(stream_select, "_audio_langs_of", lambda cfg, ch: {"eng"})  # fallback only
    monkeypatch.setattr(stream_select, "_reselect_for_primary", lambda *a, **k: None)
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    v = stream_select.prepare_stream(
        cfg, [chosen], _gopts(), auto=True, reselect_on_wrong_audio=True
    )
    assert v is not None and v.stream is chosen and v.safety_sub_lang == "ita"
    assert "sottotitoli ita attivati" in capsys.readouterr().err
