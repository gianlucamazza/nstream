"""Unit tests for the headless `--json` subsystem (headless.py).

Moved from test_cli.py with the headless extraction: `run`/`run_auto`/`auto_play`/
`_select_meta` and the lifecycle actions (--probe/--stop/--status/-c). The cast seams
reachable through cast_flow (cast_flow.caster / cast_flow.subs / cast_flow.engine) are
identical to the interactive ones pinned in test_cli.py / test_cast_flow.py."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace

import pytest

from nstream import (
    availability,
    cast_delivery,
    cast_flow,
    headless,
    headless_play,
    picker,
    state,
    stream_select,
    subs,
    tracks,
    util,
)
from nstream.config import Config
from nstream.playback import PlaybackOutcome
from nstream.types import HistoryEntry, Meta

CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])


@pytest.fixture(autouse=True)
def _hermetic_remux_feasibility(monkeypatch):
    """Remux feasibility reads the real disk (tests run on tmpfs): off unless a test opts in.
    The zero-volume recheck doesn't wait in tests."""
    monkeypatch.setattr(cast_flow.remux, "refusal", lambda *a, **k: None)
    monkeypatch.setattr(headless_play, "_VOLUME_RECHECK_S", 0.0)


def _cast_opts(**kw):
    return replace(
        headless.PlayOpts(
            auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False
        ),
        **kw,
    )


def _plan(mode, stream, audio_index=0, real_lang="ita", verified=True):
    return cast_flow.cast_vet.CastAudioPlan(mode, stream, audio_index, real_lang, verified=verified)


def _boom(msg):
    def fail(*a, **k):
        raise AssertionError(msg)

    return fail


# --- headless mode (--json / run_auto) -------------------------------------


def _hns(**kw):
    """argparse.Namespace for a headless (--json) invocation, with safe defaults."""
    base = {
        "query": [],
        "cont": False,
        "sub_menu": False,
        "year": None,
        "season": None,
        "episode": None,
        "device": None,
        "audio_lang": None,
        "probe": False,
        "stop": False,
        "status": False,
        "browse": None,
        "volume": None,
        "explain": False,
        "pause": False,
        "resume": False,
        "seek": None,
        "follow": None,  # BooleanOptionalAction default → fire-and-return
        "movies": False,
        "series": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _hopts(cast=False, audio_lang=None):
    return headless.PlayOpts(
        auto=True, cast=cast, sub_mode=None, sub_lang=None, history=False, autoplay=False,
        audio_lang=audio_lang,
    )  # fmt: skip


def _ok(pos: float = 0.0, dur: float = 0.0, subs: bool = False):
    """A delivery stub that really delivered (ADR 0031): backends return a CastResult, and
    `headless_play` now refuses to emit `ok: true` without `started`."""
    return cast_delivery.CastResult(pos, dur, subs, started=True)


def _VETTED(s):
    return headless_play.stream_select.VettedStream(stream=s, auto=True, safety_sub_lang=None)


def _wire_movie(monkeypatch, *, name="Dune", stream=None):
    stream = stream or {
        "name": "[RD+] Torrentio\n1080p",
        "title": "Dune.2024.1080p.WEB-DL.HEVC.ITA-GRP\n👤 9 💾 8 GB",
        "url": "http://rd.example/secret-token-abc/dune.mp4",
    }
    monkeypatch.setattr(
        headless.api, "search", lambda cfg, q: [{"id": "tt1", "type": "movie", "name": name}]
    )
    monkeypatch.setattr(headless_play.api, "streams", lambda cfg, t, v: [stream])
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw: _VETTED(results[0]),
    )  # fmt: skip
    # Keep audio-language discovery hermetic (no real rank_streams/vainfo in unit tests).
    monkeypatch.setattr(
        headless_play.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    monkeypatch.setattr(
        headless_play, "auto_subs", lambda *a, **k: subs.SubsPick()
    )  # local-play path
    monkeypatch.setattr(cast_flow.subs, "auto_subs", lambda *a, **k: subs.SubsPick())  # cast path
    monkeypatch.setattr(headless_play, "device_volume", lambda device: (0.4, False))

    # Cast vetting lives in cast_vet (used by cast_flow); keep hermetic for --cast tests.
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_video",
        lambda cfg, results, chosen, exact_resolution=0, **_kw: (chosen, ""),
    )
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_container",
        lambda cfg, results, chosen, target, exact_resolution=0, **_kw: (chosen, False),
    )
    monkeypatch.setattr(cast_flow.cast_vet, "cast_container", lambda cfg, stream: "mp4")
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "cast_languages",
        lambda cfg, results, exact_resolution=0, **_kw: ("ita", "eng"),
    )
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "cast_resolver",
        lambda cfg, results, exact_resolution=0, **_kw: lambda lang: "http://u2",
    )
    return stream


def test_run_auto_movie_emits_json(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        headless_play,
        "play",
        lambda *a, **k: seen.update(played=True) or PlaybackOutcome(0.0, 0.0, "", started=True),
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and seen.get("played")
    assert out["ok"] and out["action"] == "play" and out["type"] == "movie"
    assert out["title"] == "Dune" and out["selection"] == "exact"
    assert out["stream"]["resolution"] == 1080 and out["stream"]["codec"] == "hevc"
    assert out["stream"]["codec_source"] == "release_name"
    assert out["stream"]["cached"] is True and out["stream"]["backend"] == "debrid"


def test_describe_stream_uses_probed_codec_on_mismatch():
    """Release name says x265 HEVC; a cache-hit probe (Deadpool field report) is h264."""
    url = "http://rd.example/deadpool.mp4"
    stream = {
        "name": "[RD+] Torrentio\n1080p",
        "title": "Deadpool.2016.1080p.BluRay.x265.HEVC-GRP\n👤 9 💾 8 GB",
        "url": url,
    }
    tracks.clear_cache()
    tracks._cache[url] = tracks.Tracks(video_codec="h264", codec_tag="avc1", n_video=1)
    try:
        block = headless_play.describe_stream(CFG, stream)
        assert block["codec"] == "h264"
        assert block["codec_source"] == "probed"
        assert "url" not in block and "secret" not in json.dumps(block)
    finally:
        tracks.clear_cache()


def test_describe_stream_marks_unprobed_codec_as_claimed():
    stream = {
        "name": "[RD+] Torrentio\n1080p",
        "title": "Deadpool.2016.1080p.BluRay.x265.HEVC-GRP\n👤 9 💾 8 GB",
        "url": "http://rd.example/unprobed.mp4",
    }
    tracks.clear_cache()
    block = headless_play.describe_stream(CFG, stream)
    assert block["codec"] == "hevc"
    assert block["codec_source"] == "release_name"


def test_run_auto_skips_fzf(monkeypatch):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )

    def boom(*a, **k):
        raise AssertionError("fzf called")

    monkeypatch.setattr(picker, "fzf", boom)
    monkeypatch.setattr(picker, "fzf_key", boom)
    assert headless.run_auto(CFG, _hns(query=["dune"]), _hopts()) == 0


def test_run_auto_exact_match_over_first(monkeypatch, capsys):
    metas = [
        {"id": "tt9", "type": "movie", "name": "Dune: Part Two"},
        {"id": "tt1", "type": "movie", "name": "Dune"},
    ]
    monkeypatch.setattr(headless.api, "search", lambda cfg, q: metas)
    monkeypatch.setattr(
        headless.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(headless_play, "auto_subs", lambda *a, **k: subs.SubsPick())
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert out["imdb_id"] == "tt1" and out["selection"] == "exact"


def test_run_auto_year_disambiguates(monkeypatch, capsys):
    metas = [
        {"id": "tt1984", "type": "movie", "name": "Dune", "releaseInfo": "1984"},
        {"id": "tt2021", "type": "movie", "name": "Dune", "releaseInfo": "2021"},
    ]
    monkeypatch.setattr(headless.api, "search", lambda cfg, q: metas)
    monkeypatch.setattr(
        headless.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(headless_play, "auto_subs", lambda *a, **k: subs.SubsPick())
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )
    headless.run_auto(CFG, _hns(query=["dune"], year="2021"), _hopts())
    assert json.loads(capsys.readouterr().out)["imdb_id"] == "tt2021"


def test_run_auto_id_untranslated(monkeypatch, capsys):
    monkeypatch.setattr(
        headless.api,
        "search",
        lambda cfg, q: [{"id": "tmdb:1", "type": "movie", "name": "X"}],
    )
    rc = headless.run_auto(CFG, _hns(query=["x"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["error"] == "id_untranslated"
    assert out["catalog_id"] == "tmdb:1" and "tmdb:1" in out["message"]


def test_run_auto_no_result(monkeypatch, capsys):
    monkeypatch.setattr(headless.api, "search", lambda cfg, q: [])
    rc = headless.run_auto(CFG, _hns(query=["zzz"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["error"] == "no_result"


def test_run_auto_series_season_episode(monkeypatch, capsys):
    monkeypatch.setattr(
        headless.api,
        "search",
        lambda cfg, q: [{"id": "tt2", "type": "series", "name": "Severance"}],
    )
    eps = [
        {"id": "tt2:1:1", "season": 1, "episode": 1},
        {"id": "tt2:1:2", "season": 1, "episode": 2},
    ]
    monkeypatch.setattr(headless.api, "episodes", lambda cfg, sid: eps)
    monkeypatch.setattr(
        headless.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(headless_play, "auto_subs", lambda *a, **k: subs.SubsPick())
    seen = {}
    monkeypatch.setattr(
        headless.api,
        "streams",
        lambda cfg, t, v: seen.update(vid=v) or [{"name": "x", "title": "y", "url": "u"}],
    )
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )
    headless.run_auto(CFG, _hns(query=["severance"], season=1, episode=2), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert seen["vid"] == "tt2:1:2"
    assert out["type"] == "series" and out["season"] == 1 and out["episode"] == 2


def test_run_auto_episode_not_found(monkeypatch, capsys):
    monkeypatch.setattr(
        headless.api,
        "search",
        lambda cfg, q: [{"id": "tt2", "type": "series", "name": "Severance"}],
    )
    monkeypatch.setattr(
        headless.api, "episodes", lambda cfg, sid: [{"id": "tt2:1:1", "season": 1, "episode": 1}]
    )
    rc = headless.run_auto(CFG, _hns(query=["severance"], season=5, episode=9), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "episode_not_found"
    assert {"season": 1, "episode": 1} in out["available"]


def test_run_auto_cast_device_not_found(monkeypatch, capsys):
    _wire_movie(monkeypatch)

    def boom(cfg, **k):
        raise headless.CastUnavailable("più dispositivi in rete")

    monkeypatch.setattr(headless_play, "_resolve_device", boom)

    monkeypatch.setattr(headless, "_resolve_device", boom)
    monkeypatch.setattr(
        cast_flow.caster,
        "cast",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cast called")),
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "device_not_found"


def test_run_auto_cast_emits_device(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    seen = {}
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(follow=k.get("follow")) or _ok(),
    )  # fmt: skip
    headless.run_auto(CFG, _hns(query=["dune"], device="Salotto"), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "cast" and out["device"] == "Salotto"
    assert seen["follow"] is False  # default fire-and-return


def test_run_auto_cast_remux_failure_notice(monkeypatch, capsys):
    """A failed Tier-2 remux must not degrade silently: the direct-cast fallback surfaces a
    stderr warning and the JSON `notice` (first audio track may be silent/wrong-language)."""
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    plan = cast_flow.cast_vet.CastAudioPlan("remux", stream, 1, "ita", verified=True)
    monkeypatch.setattr(cast_flow.cast_vet, "vet_cast_audio", lambda *a, **k: plan)
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", lambda *a, **k: None)  # ffmpeg failed
    seen = {}
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: seen.update(cast=True) or _ok())
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: seen.update(detached=True))
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert rc == 0 and seen.get("cast")  # degraded to a direct cast
    assert out["notice"] and "remux non riuscito" in out["notice"]
    assert "remux non riuscito" in cap.err
    # Fire-and-return handoff leaves a spawned TorrServer alive for the ongoing cast.
    assert seen.get("detached")


def test_run_auto_no_debrid_url_leak(monkeypatch, capsys):
    _wire_movie(
        monkeypatch,
        stream={
            "name": "[RD+] Torrentio\n1080p",
            "title": "Dune.2021.1080p\n💾 8 GB",
            "url": "http://rd.example/secret-token-abc123/dune.mp4",
        },
    )
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = capsys.readouterr().out
    assert "secret-token-abc123" not in out and "http" not in out


def test_run_auto_sub_menu_rejected(monkeypatch, capsys):
    rc = headless.run_auto(CFG, _hns(query=["dune"], sub_menu=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["error"] == "usage"


def test_run_auto_enriched_json_audio_fields(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert out["available_audio"] == ["ita", "eng"]
    assert out["audio_lang"] == CFG.primary  # no --audio-lang → expected/primary


def test_run_auto_audio_lang_forces_dub(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    # prepare_stream (stubbed by _wire_movie) returns the stream; stream_audio_langs
    # reports the forced dub so audio_verified is honest.
    monkeypatch.setattr(
        headless_play.stream_select, "stream_audio_langs", lambda cfg, s: frozenset({"eng"})
    )
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )
    opts = headless.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, audio_lang="eng",
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"], audio_lang="eng"), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["audio_lang"] == "eng"
    assert out["audio_verified"] is True


def test_run_auto_audio_lang_unavailable(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play.stream_select,
        "prepare_stream",
        lambda *a, **k: (_ for _ in ()).throw(
            headless_play.stream_select.AudioLangUnavailable("jpn", ("ita", "eng"))
        ),
    )
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    opts = headless.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, audio_lang="jpn",
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"], audio_lang="jpn"), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "audio_lang_unavailable"
    assert out["available_audio"] == ["ita", "eng"]


def test_run_auto_probe_lists_audio_and_subs(monkeypatch, capsys):
    monkeypatch.setattr(
        headless.api, "search", lambda cfg, q: [{"id": "tt1", "type": "movie", "name": "Dune"}]
    )
    monkeypatch.setattr(
        headless.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        headless_play.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    monkeypatch.setattr(
        headless, "available_subtitle_langs", lambda cfg, t, v: ["eng", "fre", "ita"]
    )
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    monkeypatch.setattr(
        headless_play.stream_select,
        "available_resolutions",
        lambda cfg, results, *, cast: [1080, 720],
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"], probe=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "probe"
    assert out["available_audio"] == ["ita", "eng"]
    assert out["available_subtitles"] == ["eng", "fre", "ita"]
    assert out["available_resolutions"] == [1080, 720]


def test_run_auto_cast_reports_volume(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    monkeypatch.setattr(headless_play, "device_volume", lambda device: (0.4, False))
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["volume"] == 0.4 and out["muted"] is False and out["notice"] is None


def test_run_auto_cast_warns_volume_zero(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    monkeypatch.setattr(headless_play, "device_volume", lambda device: (0.0, False))
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["volume"] == 0.0 and out["notice"] and "volume" in out["notice"].lower()


# --- completion: stop / status / browse / volume / track-accurate ----------


def test_run_auto_stop(monkeypatch, capsys):
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless.caster, "stop", lambda device: True)
    rc = headless.run_auto(CFG, _hns(stop=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "stop" and out["device"] == "192.168.1.5"


def test_run_auto_status(monkeypatch, capsys):
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        headless.caster, "status",
        lambda device: {"player_state": "PLAYING", "title": "X", "position": 12.0,
                        "duration": 100.0, "volume": 0.4, "muted": False},
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(status=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "status" and out["player_state"] == "PLAYING"
    assert out["title"] == "X" and out["volume"] == 0.4


def test_run_auto_stop_device_not_found(monkeypatch, capsys):
    def boom(cfg, **k):
        raise headless.CastUnavailable("nessun Chromecast")

    monkeypatch.setattr(headless_play, "_resolve_device", boom)

    monkeypatch.setattr(headless, "_resolve_device", boom)
    rc = headless.run_auto(CFG, _hns(stop=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "device_not_found"


def test_run_auto_browse(monkeypatch, capsys):
    monkeypatch.setattr(
        headless.api, "browse", lambda cfg, cat: [{"id": "tt1", "type": "movie", "name": "Popular"}]
    )
    monkeypatch.setattr(
        headless.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(
        headless_play.stream_select, "audio_languages", lambda cfg, results, *, cast: ("eng",)
    )
    monkeypatch.setattr(headless_play, "auto_subs", lambda *a, **k: subs.SubsPick())
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )
    rc = headless.run_auto(CFG, _hns(browse="popolari"), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["title"] == "Popular" and out["selection"] == "browse"


def test_run_auto_cast_sets_volume(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    seen = {}
    monkeypatch.setattr(
        headless.caster, "set_volume", lambda device, level: seen.update(level=level)
    )
    monkeypatch.setattr(headless_play, "device_volume", lambda device: (0.35, False))
    headless.run_auto(CFG, _hns(query=["dune"], volume=35), _hopts(cast=True))
    assert seen["level"] == 35


def test_run_auto_audio_lang_not_in_real_tracks(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    # Name tags claim ita, but real-track verification rejects every candidate.
    monkeypatch.setattr(
        headless_play.stream_select,
        "prepare_stream",
        lambda *a, **k: (_ for _ in ()).throw(
            headless_play.stream_select.AudioLangUnavailable(
                "ita", ("ita", "eng"), real_tracks=True
            )
        ),
    )
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    opts = headless.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, audio_lang="ita",
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"], audio_lang="ita"), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "audio_lang_unavailable"
    assert "tracce reali" in out["message"]


# --- headless title resolution (_select_meta) --------------------------------


def test_select_meta_want_series_prefers_series():
    # An explicit --season/--episode means a series: a same-named movie (e.g. the 2026
    # Korean film "Mr. Robot") must not shadow the series the caller asks an episode of.
    movie = Meta(id="tt2", type="movie", name="Mr. Robot")
    series = Meta(id="tt1", type="series", name="Mr. Robot")
    meta, how = headless._select_meta([movie, series], "mr robot", None, want_series=True)
    assert meta["id"] == "tt1"
    assert how == "exact"


def test_select_meta_want_series_falls_back_to_movies():
    # No series in the results → degrade to the normal pick instead of failing.
    movie = Meta(id="tt2", type="movie", name="Mr. Robot")
    meta, _ = headless._select_meta([movie], "mr robot", None, want_series=True)
    assert meta["id"] == "tt2"


def test_select_meta_default_keeps_first_exact():
    movie = Meta(id="tt2", type="movie", name="Mr. Robot")
    series = Meta(id="tt1", type="series", name="Mr. Robot")
    meta, _ = headless._select_meta([movie, series], "mr robot", None)
    assert meta["id"] == "tt2"  # no season/episode hint → existing behaviour unchanged


# --- ADR 0030: the year filters the whole pool, and an explicit one is hard ---


# The live 2026-08-08 regression: an Italian query matches no English catalog name, so the
# year is the only usable evidence. It used to be discarded and "Cash Truck" (2004) was cast.
_CASH = Meta(id="tt_cash", type="movie", name="Cash Truck", releaseInfo="2004")
_LIVES = Meta(id="tt_lives", type="movie", name="The Lives of Others", releaseInfo="2006")


def test_select_meta_year_selects_across_localized_title():
    meta, how = headless._select_meta([_CASH, _LIVES], "le vite degli altri", "2006")
    assert meta["id"] == "tt_lives"
    assert how == "year"  # the year disambiguated, the name never matched


def test_select_meta_explicit_year_refuses_wrong_film():
    # Every candidate has a KNOWN, different year → refuse rather than guess.
    with pytest.raises(headless.YearMismatch) as e:
        headless._select_meta([_CASH, _LIVES], "le vite degli altri", "1999")
    assert e.value.years == ("2004", "2006")


def test_select_meta_missing_release_info_never_refuses():
    # Absence of evidence is not evidence of the wrong year: unknown spans stay eligible.
    a = Meta(id="tt1", type="movie", name="Senza Anno")
    b = Meta(id="tt2", type="movie", name="Altro", releaseInfo="")
    meta, how = headless._select_meta([a, b], "qualcosa", "2006")
    assert meta["id"] == "tt1" and how == "first"


def test_select_meta_partial_unknown_pool_is_not_a_refusal():
    # One candidate provably wrong, one unknown → fall through on the unknown, no raise.
    unknown = Meta(id="tt_u", type="movie", name="Ignoto")
    meta, how = headless._select_meta([_CASH, unknown], "ignoto", "2006")
    assert meta["id"] == "tt_u" and how == "exact"


@pytest.mark.parametrize("info", ["2006-2010", "2006–2010", "2006-"])
def test_select_meta_year_range_matches_series(info):
    # Cinemeta emits closed, en-dash and open-ended ranges for a running series.
    show = Meta(id="tt_s", type="series", name="Show", releaseInfo=info)
    other = Meta(id="tt_o", type="series", name="Other", releaseInfo="1999")
    meta, _ = headless._select_meta([other, show], "show", "2008")
    assert meta["id"] == "tt_s"


def test_select_meta_inferred_year_is_soft():
    # "2049" is part of the title, not a filter: it must never refuse or empty the pool.
    br = Meta(id="tt_br", type="movie", name="Blade Runner 2049", releaseInfo="2017")
    meta, how = headless._select_meta([br], "blade runner 2049", None)
    assert meta["id"] == "tt_br" and how == "exact"


def test_select_meta_accent_folded_match():
    # api.norm_text is now the single normalizer: NFKD folding on every path.
    amelie = Meta(id="tt_a", type="movie", name="Amélie", releaseInfo="2001")
    meta, how = headless._select_meta([amelie], "amelie", None)
    assert meta["id"] == "tt_a" and how == "exact"


def test_run_auto_year_mismatch_emits_no_result(monkeypatch, capsys):
    monkeypatch.setattr(headless.api, "search", lambda cfg, q: [dict(_CASH), dict(_LIVES)])
    rc = headless.run_auto(CFG, _hns(query=["le", "vite", "degli", "altri"], year="1999"), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["error"] == "no_result"
    assert out["years"] == ["2004", "2006"]


# --- explicit --movies/--series type flags ----------------------------------

_FARGO = [
    {"id": "ttm", "type": "movie", "name": "Fargo"},
    {"id": "tts", "type": "series", "name": "Fargo"},
]


def _wire_fargo(monkeypatch):
    """Same-title movie + series, with enough plumbing to reach the final JSON."""
    monkeypatch.setattr(headless.api, "search", lambda cfg, q: list(_FARGO))
    monkeypatch.setattr(
        headless.api, "episodes", lambda cfg, sid: [{"id": "tts:1:1", "season": 1, "episode": 1}]
    )
    monkeypatch.setattr(
        headless.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(headless_play, "auto_subs", lambda *a, **k: subs.SubsPick())
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )


def test_run_auto_series_flag_picks_series_over_same_title_movie(monkeypatch, capsys):
    """--series drops the same-named movie before the title match (explicit beats order)."""
    _wire_fargo(monkeypatch)
    rc = headless.run_auto(CFG, _hns(query=["fargo"], series=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["imdb_id"] == "tts" and out["type"] == "series"


def test_run_auto_movies_flag_picks_movie(monkeypatch, capsys):
    _wire_fargo(monkeypatch)
    rc = headless.run_auto(CFG, _hns(query=["fargo"], movies=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["imdb_id"] == "ttm" and out["type"] == "movie"


def test_run_auto_season_inference_unchanged_without_flags(monkeypatch, capsys):
    """No explicit flag → --season still infers the series (pre-flag behaviour)."""
    _wire_fargo(monkeypatch)
    rc = headless.run_auto(CFG, _hns(query=["fargo"], season=1, episode=1), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["imdb_id"] == "tts" and out["season"] == 1


def test_run_auto_movies_with_season_is_usage_error(monkeypatch, capsys):
    """--movies + --season/--episode is contradictory → usage error, nothing fetched."""
    monkeypatch.setattr(
        headless.api, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("searched"))
    )
    rc = headless.run_auto(CFG, _hns(query=["fargo"], movies=True, season=1), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["error"] == "usage" and "--movies" in out["message"]


def test_run_auto_series_flag_no_result_reflects_filter(monkeypatch, capsys):
    """When the filter empties the results, the error message names the type."""
    monkeypatch.setattr(
        headless.api, "search", lambda cfg, q: [{"id": "ttm", "type": "movie", "name": "Fargo"}]
    )
    rc = headless.run_auto(CFG, _hns(query=["fargo"], series=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "no_result" and "serie" in out["message"]


# --- headless cast tree (auto_play) — safety net for the headless extraction ---


def test_run_auto_mirror_action(monkeypatch, capsys):
    """Headless twin of the mirror gate: --json --cast --mirror + plan remux → action=mirror."""
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_audio",
        lambda *a, **k: _plan("remux", stream, audio_index=1),
    )
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    seen = {}
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror",
        lambda cfg, title, url, **k: (
            seen.update(url=url, follow=k.get("follow")) or _ok()
        ),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("mirror must preempt the remux"))
    monkeypatch.setattr(
        cast_flow.caster, "cast", _boom("direct cast must not run on the mirror path")
    )
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _cast_opts(mirror=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "mirror" and out["reencoded"] is False
    assert seen["url"] == stream["url"] and seen["follow"] is False  # fire-and-return default


def test_run_auto_cast_remux_success_reencoded(monkeypatch, capsys):
    """Headless Tier-2 success: JSON says reencoded; fire-and-return uses cast_file with
    follow=False and no event callback, while --follow wires the JSONL callback in."""
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_audio",
        lambda *a, **k: _plan("remux", stream, audio_index=1),
    )
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", lambda *a, **k: "/tmp/out.mp4")
    seen: dict = {"detached": 0}
    monkeypatch.setattr(
        cast_flow.remux, "cast_file",
        lambda cfg, title, path, **k: (
            seen.update(path=path, follow=k.get("follow"), on_event=k.get("on_event"))
            or _ok()
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.caster, "cast", _boom("direct cast must not run when the remux succeeds")
    )
    monkeypatch.setattr(
        cast_flow.engine, "detach_spawned", lambda: seen.update(detached=seen["detached"] + 1)
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _cast_opts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["reencoded"] is True and out["action"] == "cast"
    assert seen["path"] == "/tmp/out.mp4"
    assert seen["follow"] is False and seen["on_event"] is None  # fire-and-return default
    assert seen["detached"] == 1  # handoff keeps a spawned TorrServer alive
    rc = headless.run_auto(CFG, _hns(query=["dune"], follow=True), _cast_opts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["reencoded"] is True
    assert seen["follow"] is True and callable(seen["on_event"])  # JSONL events wired
    assert seen["detached"] == 1  # --follow keeps ownership: no detach


def test_run_auto_cast_absent_safety_subs_json(monkeypatch, capsys):
    """Headless absent dub: the JSON reports the real language + the safety subtitles."""
    stream = _wire_movie(monkeypatch)
    calls = []
    monkeypatch.setattr(
        cast_flow.subs, "auto_subs",
        lambda cfg, typ, vid, wd, opts, safety_sub_lang=None, **kw: (
            calls.append(safety_sub_lang) or subs.SubsPick(("/tmp/sub.srt",), "lang")
        ),
    )  # fmt: skip
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_audio",
        lambda *a, **k: _plan("absent", stream, real_lang="eng"),
    )
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("no remux for an absent language"))
    seen = {}
    monkeypatch.setattr(
        cast_flow.caster, "cast", lambda *a, **k: seen.update(cast=True) or _ok(subs=True)
    )
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _cast_opts())
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert rc == 0 and seen.get("cast") is True
    assert out["audio_lang"] == "eng" and out["audio_verified"] is True
    assert out["subtitles"] == CFG.primary  # safety subs reported in the JSON
    # cast_flow normalization: auto_subs runs exactly ONCE, with the safety language
    # (the old headless copy called it twice in the absent branch).
    assert calls == [CFG.primary]
    # cast_flow normalization: the absent notice is printed on the headless path too.
    assert "non disponibile" in cap.err


def test_run_auto_follow_emits_event_jsonl(monkeypatch, capsys):
    """--json --cast --follow: each castbridge event becomes one JSONL line on stdout."""
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        cast_flow.cast_vet, "vet_cast_audio", lambda *a, **k: _plan("direct", stream)
    )
    seen = {}

    def fake_cast(cfg, title, url, **k):
        seen["follow"] = k.get("follow")
        k["on_event"]({"kind": "playing", "position": 3.0})  # the callback IS the JSONL writer
        return _ok()

    monkeypatch.setattr(cast_flow.caster, "cast", fake_cast)
    rc = headless.run_auto(CFG, _hns(query=["dune"], follow=True), _cast_opts())
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 0 and seen["follow"] is True
    event, final = lines[0], lines[-1]
    assert event["event"] == "playing" and event["ok"] is True and event["position"] == 3.0
    assert final["action"] == "cast" and final["error"] is None


# --- resume / headless-continue perimeter — safety net for the headless extraction ---


def test_run_auto_resume_threads_history_entry(monkeypatch):
    """--json -c with a query: the matched series entry's identity reaches auto_play."""
    entry = HistoryEntry(
        video_id="tt2:1:2", title="Severance", type="series", series_id="tt2",
        season=1, episode=2, position=100.0, duration=3000.0, ts=1.0,
    )  # fmt: skip
    monkeypatch.setattr(headless.state, "resumable", lambda cfg, limit=30, typ=None: [entry])
    seen = {}

    def spy(cfg, args, opts, typ, video_id, title, imdb_id, season, episode, selection, **kw):
        seen.update(
            typ=typ, video_id=video_id, title=title, imdb_id=imdb_id,
            season=season, episode=episode, selection=selection,
        )  # fmt: skip
        return 0

    monkeypatch.setattr(headless.headless_play, "auto_play", spy)
    rc = headless.run_auto(CFG, _hns(query=["severance"], cont=True), _hopts())
    assert rc == 0
    assert seen["typ"] == "series" and seen["video_id"] == "tt2:1:2"
    assert seen["imdb_id"] == "tt2" and seen["season"] == 1 and seen["episode"] == 2
    assert seen["selection"] == "resume" and seen["title"] == "Severance · S01E02"


def test_run_auto_resume_no_result(monkeypatch, tmp_path, capsys):
    """--json -c: empty history and a no-match query are both no_result (rc 1)."""
    # Isolate the on-disk history: the resume path reads it directly (not only via the stubbed
    # `state.recent`), so without this it would find the real user history and try to play it.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(headless.headless_play, "auto_play", _boom("nothing should play"))
    monkeypatch.setattr(headless.state, "recent", lambda cfg, limit=30, typ=None: [])
    rc = headless.run_auto(CFG, _hns(cont=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "no_result" and "cronologia vuota" in out["message"]
    entry = HistoryEntry(video_id="tt3", title="Dune", type="movie", ts=1.0)
    monkeypatch.setattr(headless.state, "resumable", lambda cfg, limit=30, typ=None: [entry])
    rc = headless.run_auto(CFG, _hns(query=["matrix"], cont=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "no_result" and "matrix" in out["message"]


# --- watch history on the headless surface (M1: headless never wrote it) ----


def _hist_opts(cast):
    return headless.PlayOpts(
        auto=True, cast=cast, sub_mode=None, sub_lang=None, history=True, autoplay=False
    )


def test_local_play_saves_history(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(600.0, 6000.0, "", started=True)
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hist_opts(cast=False))
    assert rc == 0
    e = headless.state.load_history(CFG)["tt1"]
    assert (e["position"], e["duration"], e["title"]) == (600.0, 6000.0, "Dune")


def test_follow_cast_saves_history(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok(600.0, 6000.0))
    rc = headless.run_auto(CFG, _hns(query=["dune"], follow=True), _hist_opts(cast=True))
    assert rc == 0
    e = headless.state.load_history(CFG)["tt1"]
    assert (e["position"], e["duration"]) == (600.0, 6000.0)


def test_fire_and_return_notes_started_and_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hist_opts(cast=True))
    assert rc == 0
    # started entry: the title is now known to -c (no more restart-at-S01E01)
    e = headless.state.load_history(CFG)["tt1"]
    assert (e["position"], e["duration"], e["title"]) == (0.0, 0.0, "Dune")
    # cast session: --stop/--status can attribute the receiver position to it
    session = util.RunState(headless.state.CAST_SESSION).read()
    assert session is not None
    assert session["video_id"] == "tt1" and session["device"] == "192.168.1.5"


def test_stop_persists_receiver_position_and_counts_remux(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    headless.state.remember_cast(
        CFG, headless.state.make_entry("tt9", "Dune", "movie", 0.0, 0.0), "192.168.1.5"
    )
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.mirror, "stop", lambda: False)
    monkeypatch.setattr(
        headless.caster, "status",
        lambda device: {"player_state": "PLAYING", "title": "Dune", "position": 1000.0,
                        "duration": 5000.0, "volume": 0.4, "muted": False},
    )  # fmt: skip
    monkeypatch.setattr(headless.caster, "stop", lambda device: False)  # TV unreachable…
    monkeypatch.setattr(cast_flow.remux, "stop", lambda device: True)  # …but remux reclaimed
    rc = headless.run_auto(CFG, _hns(stop=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True  # remux.stop success counts (L5)
    e = headless.state.load_history(CFG)["tt9"]
    assert (e["position"], e["duration"]) == (1000.0, 5000.0)
    assert util.RunState(headless.state.CAST_SESSION).read() is None  # one-shot


def test_status_refreshes_session_entry(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    headless.state.remember_cast(
        CFG, headless.state.make_entry("tt9", "Dune", "movie", 0.0, 0.0), "192.168.1.5"
    )
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        headless.caster, "status",
        lambda device: {"player_state": "PLAYING", "title": "Dune", "position": 700.0,
                        "duration": 5000.0, "volume": 0.4, "muted": False},
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(status=True), _hopts(cast=True))
    assert rc == 0
    e = headless.state.load_history(CFG)["tt9"]
    assert e["position"] == 700.0
    # session kept: the next poll keeps refreshing the resume point
    assert util.RunState(headless.state.CAST_SESSION).read() is not None


def test_subtitles_not_reported_when_delivery_drops_them(monkeypatch, capsys):
    """M3: subs the castbridge LOAD can't carry must not be claimed in the JSON."""
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(
        cast_flow.subs, "auto_subs", lambda *a, **k: subs.SubsPick(("/tmp/sub.srt",), "lang")
    )
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_audio",
        lambda *a, **k: _plan("absent", stream, real_lang="eng"),
    )
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("no remux here"))
    # bridge path: subs_delivered=False
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _cast_opts())
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert rc == 0 and out["subtitles"] is None
    assert "non caricati sul TV" in cap.err  # honesty notice on stderr


def test_auto_play_session_stores_resolved_ip(monkeypatch, tmp_path, capsys):
    """Regression (cold review, HIGH): with cast_device configured the session must be
    keyed by the resolved IP — --stop/--status compare against resolve_device()'s IP,
    so a name key ("Salotto") would never match and the merge would be inert."""
    cfg = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"], cast_device="Salotto")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda c, **k: "192.168.1.9")
    monkeypatch.setattr(headless, "_resolve_device", lambda c, **k: "192.168.1.9")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    rc = headless.run_auto(cfg, _hns(query=["dune"]), _hist_opts(cast=True))
    assert rc == 0
    session = util.RunState(headless.state.CAST_SESSION).read()
    assert session is not None
    assert session["device"] == "192.168.1.9"  # the IP, not "Salotto"
    # …and the loop closes: a later --stop with the resolved IP merges the position
    monkeypatch.setattr(cast_flow.mirror, "stop", lambda: False)
    monkeypatch.setattr(
        headless.caster, "status",
        lambda device: {"player_state": "PLAYING", "title": "Dune", "position": 900.0,
                        "duration": 5000.0, "volume": 0.4, "muted": False},
    )  # fmt: skip
    monkeypatch.setattr(headless.caster, "stop", lambda device: True)
    monkeypatch.setattr(cast_flow.remux, "stop", lambda device: False)
    rc = headless.run_auto(cfg, _hns(stop=True), _hopts(cast=True))
    assert rc == 0
    assert headless.state.load_history(cfg)["tt1"]["position"] == 900.0


def test_run_status_title_mismatch_no_merge(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    headless.state.remember_cast(
        CFG, headless.state.make_entry("tt9", "Dune", "movie", 0.0, 0.0), "192.168.1.5"
    )
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        headless.caster, "status",
        lambda device: {"player_state": "PLAYING", "title": "Peppa Pig", "position": 300.0,
                        "duration": 900.0, "volume": 0.4, "muted": False},
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(status=True), _hopts(cast=True))
    assert rc == 0
    assert headless.state.load_history(CFG) == {}  # foreign position NOT attributed
    assert util.RunState(headless.state.CAST_SESSION).read() is None


# --- quick-win lifecycle actions (pause/resume/seek, standalone volume, episodes) ----


def test_run_control_pause_via_bridge(monkeypatch, capsys):
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless.bridge, "bridge_available", lambda: True)
    seen = {}
    monkeypatch.setattr(
        headless.bridge, "control",
        lambda ip, cmd, value=0.0: seen.update(ip=ip, cmd=cmd, value=value) or True,
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(pause=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True and out["action"] == "pause"
    assert seen == {"ip": "192.168.1.5", "cmd": "pause", "value": 0.0}


def test_run_control_seek_falls_back_to_catt(monkeypatch, capsys):
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    # conftest: bridge_available is already False → catt path
    calls = []

    class _Res:
        returncode = 0

    monkeypatch.setattr(headless.util, "run_cmd", lambda a, **k: calls.append(a) or _Res())
    rc = headless.run_auto(CFG, _hns(seek=125.0), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "seek" and out["seek"] == 125.0
    assert calls == [["catt", "-d", "192.168.1.5", "seek", "125"]]


def test_run_control_resume_failure_sets_error(monkeypatch, capsys):
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless.util, "run_cmd", lambda a, **k: None)  # catt missing
    rc = headless.run_auto(CFG, _hns(resume=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["error"] == "control_failed"


def test_run_volume_standalone(monkeypatch, capsys):
    """--json --volume N with no title acts on the current cast (it used to require
    re-casting a whole title)."""
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    seen = {}
    monkeypatch.setattr(
        headless.caster, "set_volume", lambda device, n: seen.update(device=device, n=n) or True
    )
    rc = headless.run_auto(CFG, _hns(volume=35), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "volume"
    assert out["volume"] == 0.35 and out["volume_percent"] == 35
    assert seen == {"device": "192.168.1.5", "n": 35}


def test_volume_with_title_still_casts(monkeypatch, capsys):
    """--volume alongside a title keeps the historical start-volume semantics."""
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    seen = {}
    monkeypatch.setattr(headless.caster, "set_volume", lambda device, n: seen.update(n=n) or True)
    rc = headless.run_auto(CFG, _hns(query=["dune"], volume=40), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "cast"
    assert seen == {"n": 40}


def test_probe_series_without_episode_lists_episodes(monkeypatch, capsys):
    monkeypatch.setattr(
        headless.api, "search", lambda cfg, q: [{"id": "tt1", "type": "series", "name": "Show"}]
    )
    monkeypatch.setattr(
        headless.api, "episodes",
        lambda cfg, sid: [
            {"id": "tt1:1:1", "season": 1, "episode": 1, "name": "Pilot"},
            {"id": "tt1:1:2", "season": 1, "episode": 2, "name": "Two"},
            {"id": "tt1:2:1", "season": 2, "episode": 1, "name": "S2 opener"},
        ],
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["show"], probe=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "episodes"
    assert [e["episode"] for e in out["episodes"]] == [1, 2, 1]
    # --season narrows
    rc = headless.run_auto(CFG, _hns(query=["show"], probe=True, season=2), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert [(e["season"], e["title"]) for e in out["episodes"]] == [(2, "S2 opener")]


# --- auto-advance: -c on a finished episode casts the next one ---------------


def _wire_advance(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    headless.state.save_entry(
        CFG,
        headless.state.make_entry(
            "tt1:1:4", "Show", "series", 2950.0, 3000.0, series_id="tt1", season=1, episode=4
        ),
    )
    monkeypatch.setattr(
        headless.api, "episodes",
        lambda cfg, sid: [
            {"id": "tt1:1:4", "season": 1, "episode": 4, "name": "Four"},
            {"id": "tt1:1:5", "season": 1, "episode": 5, "name": "Five"},
        ],
    )  # fmt: skip


def test_resume_advances_to_next_episode(monkeypatch, tmp_path, capsys):
    _wire_advance(monkeypatch, tmp_path)
    played = {}

    def fake_auto_play(
        cfg,
        args,
        opts,
        typ,
        video_id,
        title,
        imdb_id,
        season,
        episode,
        selection,
        cast_meta=None,
        *,
        name=None,
    ):
        played.update(video_id=video_id, season=season, episode=episode, selection=selection)
        return 0

    monkeypatch.setattr(headless.headless_play, "auto_play", fake_auto_play)
    rc = headless.run_auto(CFG, _hns(cont=True, query=["show"]), _hopts())
    assert rc == 0
    assert played == {"video_id": "tt1:1:5", "season": 1, "episode": 5, "selection": "next"}


def test_resume_series_completed(monkeypatch, tmp_path, capsys):
    _wire_advance(monkeypatch, tmp_path)
    monkeypatch.setattr(
        headless.api,
        "episodes",
        lambda cfg, sid: [{"id": "tt1:1:4", "season": 1, "episode": 4, "name": "Four"}],
    )
    rc = headless.run_auto(CFG, _hns(cont=True, query=["show"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "series_completed"
    assert "S01E04" in out["message"]


def test_resume_prefers_in_progress_over_advance(monkeypatch, tmp_path, capsys):
    """A partially-watched episode still resumes itself; advance only kicks in when
    the matched episode is finished."""
    _wire_advance(monkeypatch, tmp_path)
    headless.state.save_entry(
        CFG,
        headless.state.make_entry(
            "tt1:1:5", "Show", "series", 600.0, 3000.0, series_id="tt1", season=1, episode=5
        ),
    )
    played = {}

    def fake_auto_play(
        cfg,
        args,
        opts,
        typ,
        video_id,
        title,
        imdb_id,
        season,
        episode,
        selection,
        cast_meta=None,
        *,
        name=None,
    ):
        played.update(video_id=video_id, selection=selection)
        return 0

    monkeypatch.setattr(headless.headless_play, "auto_play", fake_auto_play)
    rc = headless.run_auto(CFG, _hns(cont=True, query=["show"]), _hopts())
    assert rc == 0
    assert played == {"video_id": "tt1:1:5", "selection": "resume"}


def test_json_explain_is_read_only_and_structured(monkeypatch, capsys):
    """--json --explain must diagnose, not play (it used to fall through to the cast
    path), and emit the ranking as structured data."""
    stream = {
        "name": "[RD+] Torrentio\n1080p",
        "title": "Dune.2024.1080p.WEB-DL.HEVC.ITA-GRP\n👤 9 💾 8 GB",
        "url": "http://rd.example/secret-token-abc/dune.mp4",
    }
    monkeypatch.setattr(
        headless.api, "search", lambda cfg, q: [{"id": "tt1", "type": "movie", "name": "Dune"}]
    )
    monkeypatch.setattr(headless_play.api, "streams", lambda cfg, t, v: [stream])
    monkeypatch.setattr(headless_play, "play", _boom("explain must never play"))
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("explain must never cast"))
    monkeypatch.setattr(headless_play.stream_select, "prepare_stream", _boom("no vetting either"))
    rc = headless.run_auto(CFG, _hns(query=["dune"], explain=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "explain" and out["profile"] == "cast"
    assert out["counts"]["total"] == 1
    assert out["pick"] is None or "secret-token" not in json.dumps(out)  # never the url
    row = (out["playable"] + out["excluded"])[0]
    assert row["resolution"] == 1080 and "score" in row


# --- quality filter (headless) -----------------------------------------------


def test_run_auto_quality_unavailable(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play.stream_select,
        "available_resolutions",
        lambda cfg, results, *, cast: [2160, 720],
    )
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    opts = headless.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, quality=1080,
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"]), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "quality_unavailable"
    assert out["available_resolutions"] == [2160, 720]


def test_run_auto_quality_echoes_on_success(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play.stream_select, "available_resolutions", lambda cfg, results, *, cast: [1080]
    )

    def prep(cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw):
        return headless_play.stream_select.VettedStream(
            stream=results[0], auto=True, safety_sub_lang=None, quality=opts.quality or 0
        )

    monkeypatch.setattr(headless_play.stream_select, "prepare_stream", prep)
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )
    opts = headless.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, quality=1080,
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"]), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["quality"] == 1080
    assert out["available_resolutions"] == [1080]
    assert out["stream"]["resolution"] == 1080


def test_cast_boundary_forwards_resolved_quality_to_run_cast(monkeypatch, capsys):
    """ADR 0021 boundary: run_cast must receive opts with the RESOLVED VettedStream
    quality (the reselect paths depend on it), even though the caller's opts.quality
    was None on entry."""
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw: (
            headless_play.stream_select.VettedStream(
                stream=results[0], auto=True, safety_sub_lang=None, quality=1080
            )
        ),
    )  # fmt: skip
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    seen = {}

    def fake_run_cast(cfg, results, chosen, **kw):
        seen["quality"] = kw["opts"].quality
        return cast_flow.CastOutcome(
            pos=0.0, dur=0.0, advance=False, action="cast", stream=chosen,
            reencoded=False, notice=None, audio_lang="ita", audio_verified=True,
            safety_sub_lang=None, sub_paths=(), sub_match=None, sub_offset=None,
            subs_delivered=False, started=True, cast_error=None,
        )  # fmt: skip

    monkeypatch.setattr(cast_flow, "run_cast", fake_run_cast)
    monkeypatch.setattr(headless.caster, "device_volume", lambda d: (0.5, False))
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _cast_opts())
    assert rc == 0 and seen["quality"] == 1080


# --- sources removed from the debrid (ADR 0025) ----------------------------


def test_run_auto_sources_removed_when_denylisted(monkeypatch, capsys, tmp_path):
    """Every known source for the title was proven removed in an earlier run: say so, so the
    caller doesn't suggest a pointless retry."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch, stream={"name": "x\n1080p", "infoHash": "DEAD9", "url": "http://rd/x"})
    state.mark_dead(
        availability.source_key({"infoHash": "DEAD9", "url": "http://rd/x"}), "HTTP 404"
    )
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    cfg = Config(torrentio_base="tb", playback_backend="debrid")
    rc = headless.run_auto(cfg, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "sources_removed"
    assert out["removed_sources"] == 1


def test_run_auto_sources_removed_when_verification_proves_them_gone(monkeypatch, capsys, tmp_path):
    """The pre-commit verification itself proves the last candidates gone: same honest error,
    not a generic no_playable_stream."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch, stream={"name": "x\n1080p", "infoHash": "G7", "url": "http://rd/x"})

    def prep(cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw):
        state.mark_dead(
            availability.source_key({"infoHash": "G7", "url": "http://rd/x"}), "HTTP 404"
        )  # what _verify_availability does on a `gone` verdict
        results[:] = []
        return None

    monkeypatch.setattr(headless_play.stream_select, "prepare_stream", prep)
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "sources_removed"


def test_run_auto_empty_set_without_proof_is_not_sources_removed(monkeypatch, capsys, tmp_path):
    """An unusable-right-now source (incomplete transfer, flaky link) empties the candidate
    list too — but nothing was proven removed, so the caller must not be told it was."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch, stream={"name": "x\n1080p", "infoHash": "T7", "url": "http://rd/x"})

    def prep(cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw):
        results[:] = []  # dropped for this run only
        return None

    monkeypatch.setattr(headless_play.stream_select, "prepare_stream", prep)
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "no_playable_stream"


def test_run_auto_no_playable_stream_still_reported(monkeypatch, capsys, tmp_path):
    """A pick that fails for other reasons (hw filter) keeps the old error."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio, title="", **_kw: None,
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "no_playable_stream"


def _raise(exc):
    def boom(*a, **k):
        raise exc

    return boom


def test_run_auto_cast_unresolved_reports_no_playable_stream(monkeypatch, capsys, tmp_path):
    """A settled cast stream with no url is a retry-worthy source problem, not an internal
    crash: the field run surfaced it as `{"error": "internal"}` (ADR 0031 appendix)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        headless_play.cast_flow, "run_cast", _raise(cast_flow.CastStreamUnresolved())
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"], device="Salotto"), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["error"] == "no_playable_stream"


def test_run_auto_cast_failure_emits_cast_failed(monkeypatch, capsys, tmp_path):
    """The live 2026-08-08 contract violation: catt printed "cast non riuscito" on stderr
    and the JSON still said `ok: true`, exit 0. The backend now reports `started=False`,
    and no phantom cast session is recorded for a cast that never played (ADR 0031)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: cast_delivery.CastResult(0.0, 0.0, error="cast_failed"),
    )  # fmt: skip
    monkeypatch.setattr(
        headless_play.state, "remember_cast", _raise(AssertionError("phantom cast session"))
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"], device="Salotto"), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False
    assert out["error"] == "cast_failed" and out["cast_error"] == "cast_failed"
    assert out["device"] == "Salotto"


# --- content duration vetting (ADR 0028) -----------------------------------


def _too_short(duration=30.0, expected=3300.0, count=2):
    verdict = availability.DurationVerdict(
        False, duration=duration, expected=expected, reason="durata 0:30 contro ~55 min attesi"
    )
    return stream_select.ContentTooShort(verdict, count)


def test_json_reports_sources_truncated(monkeypatch, capsys, tmp_path):
    """A placeholder is not `no_playable_stream`: that code is documented as retriable,
    and retrying a 30s file yields the same 30s file."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda *a, **k: (_ for _ in ()).throw(_too_short()),
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["error"] == "sources_truncated"
    # Both measures in clear, so an implausible expected runtime is visible at a glance.
    assert out["duration_s"] == 30.0 and out["expected_runtime_s"] == 3300.0
    assert out["truncated_sources"] == 2


def test_json_no_playable_stream_carries_the_reason(monkeypatch, capsys, tmp_path):
    """The caller (skill/agent) must learn *why* — a blocked P2P gate is actionable,
    a bare `no_playable_stream` is not."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch)
    reason = "solo sorgenti torrent e streaming P2P bloccato: nessuna VPN"
    monkeypatch.setattr(
        headless_play.stream_select, "prepare_stream",
        lambda *a, **k: (_ for _ in ()).throw(stream_select.NoPlayableStream(reason)),
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "no_playable_stream" and reason in out["message"]


def test_audio_lang_short_file_is_not_reported_as_lang_unavailable(monkeypatch, capsys, tmp_path):
    """The incident's exact path: with --audio-lang, truncated sources must not degrade to
    `audio_lang_unavailable` — that would send the caller chasing a dub that exists."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play.stream_select,
        "prepare_stream",
        lambda *a, **k: (_ for _ in ()).throw(_too_short(count=1)),
    )  # fmt: skip
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts(audio_lang="ita"))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "sources_truncated"


def test_json_success_reports_duration_verified(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless_play, "play", lambda *a, **k: PlaybackOutcome(0.0, 0.0, "", started=True)
    )
    monkeypatch.setattr(headless_play.api, "expected_runtime_s", lambda cfg, t, v: 8160.0)
    # The guard measured this file earlier in the run (memoized probe) → verified.
    monkeypatch.setattr(headless_play.tracks, "cached_duration", lambda url: 8100.0)
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["duration_verified"] is True
    # Unknown runtime → the guard never ran → null, never a bare `true`.
    monkeypatch.setattr(headless_play.api, "expected_runtime_s", lambda cfg, t, v: 0.0)
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    assert json.loads(capsys.readouterr().out)["duration_verified"] is None
    assert stream["url"].startswith("http://")  # sanity: the fixture stream was used


# --- one continuation policy, shared with the TUI (ADR 0029) ----------------


def _spy_auto_play(monkeypatch, played):
    def fake(cfg, args, opts, typ, video_id, title, imdb_id, season, episode, selection,
             cast_meta=None, *, name=None):  # fmt: skip
        played.update(video_id=video_id, selection=selection)
        return 0

    monkeypatch.setattr(headless.headless_play, "auto_play", fake)


def test_query_prefers_the_fresh_finish_over_a_stale_half_watch(monkeypatch, tmp_path):
    """The branch bug: with a search term the in-progress list won unconditionally, so a
    half-watched episode from weeks ago beat a binge finished minutes ago."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    stale = headless.state.make_entry(
        "tt1:1:2", "Show", "series", 300.0, 3000.0, series_id="tt1", season=1, episode=2
    )
    stale["ts"] = 1.0
    fresh = headless.state.make_entry(
        "tt1:1:7", "Show", "series", 2990.0, 3000.0, series_id="tt1", season=1, episode=7
    )
    fresh["ts"] = 9.0
    for e in (stale, fresh):
        headless.state.save_entry(CFG, e)
    monkeypatch.setattr(
        headless.api, "episodes",
        lambda cfg, sid: [
            {"id": f"tt1:1:{i}", "season": 1, "episode": i} for i in range(1, 9)
        ],
    )  # fmt: skip
    played = {}
    _spy_auto_play(monkeypatch, played)
    assert headless.run_auto(CFG, _hns(cont=True, query=["show"]), _hopts()) == 0
    assert played == {"video_id": "tt1:1:8", "selection": "next"}  # continues the binge


def test_legacy_entry_without_position_never_restarts_the_series(monkeypatch, tmp_path):
    """An entry that doesn't know its season/episode used to compare as (0,0) and "advance"
    to S01E01 — silently restarting a series the user was in the middle of."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    entry = headless.state.make_entry("tt1:legacy", "Show", "series", 2990.0, 3000.0,
                                      series_id="tt1")  # fmt: skip
    headless.state.save_entry(CFG, entry)
    monkeypatch.setattr(
        headless.api, "episodes",
        lambda cfg, sid: [{"id": "tt1:1:1", "season": 1, "episode": 1}],
    )  # fmt: skip
    played = {}
    _spy_auto_play(monkeypatch, played)
    assert headless.run_auto(CFG, _hns(cont=True, query=["show"]), _hopts()) == 0
    assert played == {"video_id": "tt1:legacy", "selection": "resume"}


def test_resume_reconciles_the_cast_session_before_deciding(monkeypatch, tmp_path):
    """Fire-and-return leaves duration=0, which can never read as finished. Asking the
    receiver once — where the answer changes the decision — makes `-c` advance for real."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    entry = headless.state.make_entry(
        "tt1:1:4", "Show", "series", 0.0, 0.0, series_id="tt1", season=1, episode=4
    )
    headless.state.save_entry(CFG, entry)
    headless.state.remember_cast(CFG, entry, "192.0.2.10")
    monkeypatch.setattr(
        headless.caster, "status",
        lambda device: {"position": 2990.0, "duration": 3000.0, "title": "Show"},
    )  # fmt: skip
    monkeypatch.setattr(
        headless.api, "episodes",
        lambda cfg, sid: [
            {"id": "tt1:1:4", "season": 1, "episode": 4},
            {"id": "tt1:1:5", "season": 1, "episode": 5},
        ],
    )  # fmt: skip
    played = {}
    _spy_auto_play(monkeypatch, played)
    assert headless.run_auto(CFG, _hns(cont=True, query=["show"]), _hopts()) == 0
    assert played == {"video_id": "tt1:1:5", "selection": "next"}


def test_resume_idle_receiver_writes_no_duration(monkeypatch, tmp_path):
    """A TV switched off (or idle) reports nothing: the episode is proposed again, and no
    invented duration is written — the honest gap, not a synthetic one (ADR 0028 §6)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    entry = headless.state.make_entry(
        "tt1:1:4", "Show", "series", 0.0, 0.0, series_id="tt1", season=1, episode=4
    )
    headless.state.save_entry(CFG, entry)
    headless.state.remember_cast(CFG, entry, "192.0.2.10")
    monkeypatch.setattr(headless.caster, "status", lambda device: {})
    monkeypatch.setattr(
        headless.api, "episodes",
        lambda cfg, sid: [{"id": f"tt1:1:{i}", "season": 1, "episode": i} for i in (4, 5)],
    )  # fmt: skip
    played = {}
    _spy_auto_play(monkeypatch, played)
    assert headless.run_auto(CFG, _hns(cont=True, query=["show"]), _hopts()) == 0
    assert played == {"video_id": "tt1:1:4", "selection": "resume"}
    stored = headless.state.load_history(CFG)["tt1:1:4"]
    assert (stored.get("duration") or 0.0) == 0.0


def test_resume_without_a_cast_session_asks_no_receiver(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    headless.state.save_entry(
        CFG,
        headless.state.make_entry("tt9", "Film", "movie", 10.0, 100.0),
    )
    monkeypatch.setattr(
        headless.caster, "status", lambda device: pytest.fail("no session → no receiver call")
    )
    played = {}
    _spy_auto_play(monkeypatch, played)
    assert headless.run_auto(CFG, _hns(cont=True, query=["film"]), _hopts()) == 0
    assert played == {"video_id": "tt9", "selection": "resume"}


def test_run_auto_stop_unreachable_device_reclaims_remux(monkeypatch, capsys):
    def boom(cfg, **k):
        raise headless.CastUnavailable("nessun Chromecast")

    calls: list = []
    monkeypatch.setattr(headless, "_resolve_device", boom)
    monkeypatch.setattr(headless.mirror, "stop", lambda: False)
    monkeypatch.setattr(headless.remux, "stop", lambda dev: calls.append(dev) or True)
    rc = headless.run_auto(CFG, _hns(stop=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)  # exactly one JSON object
    assert calls == [None]
    assert rc == 0 and out["ok"] is True and out["action"] == "stop"


def test_cast_device_resolved_before_any_stream_work(monkeypatch, capsys):
    # A missing TV must fail before debrid resolves / P2P joins / probes.
    def no_tv(cfg, **k):
        raise headless.CastUnavailable("nessun Chromecast in rete")

    monkeypatch.setattr(headless_play, "_resolve_device", no_tv)
    monkeypatch.setattr(
        headless_play.api, "streams", lambda *a, **k: pytest.fail("streams fetched before device")
    )
    rc = headless_play.auto_play(
        CFG, _hns(), _hopts(cast=True), "movie", "tt1", "Dune", "tt1", None, None, "exact"
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "device_not_found"


def test_zero_volume_is_rechecked_before_warning(monkeypatch, capsys):
    # Right after a LOAD the receiver can report 0 for a moment: read again before warning.
    reads = iter([(0.0, False), (0.35, False)])
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless_play, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    monkeypatch.setattr(headless_play, "device_volume", lambda device: next(reads))
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["volume"] == 0.35 and out["notice"] is None


def test_sub_shift_on_a_non_live_cast_is_a_usage_error(monkeypatch, capsys):
    import argparse

    monkeypatch.setattr(headless, "_headless_device", lambda cfg, args: "10.0.0.5")
    monkeypatch.setattr(headless.remux, "live_sub_shift", lambda dev, d: None)
    rc = headless._run_sub_shift(CFG, argparse.Namespace(sub_shift=1.5))
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["error"] == "usage"
    monkeypatch.setattr(headless.remux, "live_sub_shift", lambda dev, d: 3.0)
    assert headless._run_sub_shift(CFG, argparse.Namespace(sub_shift=1.5)) == 0
    assert json.loads(capsys.readouterr().out)["shift"] == 3.0
