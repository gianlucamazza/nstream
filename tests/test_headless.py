"""Unit tests for the headless `--json` subsystem (headless.py).

Moved from test_cli.py with the headless extraction: `run`/`run_auto`/`_auto_play`/
`_select_meta` and the lifecycle actions (--probe/--stop/--status/-c). The cast seams
reachable through cast_flow (cast_flow.caster / cast_flow.subs / cast_flow.engine) are
identical to the interactive ones pinned in test_cli.py / test_cast_flow.py."""

from __future__ import annotations

import argparse
import json

from nstream import cast_flow, headless, picker
from nstream.config import Config, HistoryEntry, Meta

CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])


def _cast_opts(**kw):
    base = dict(auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False)
    base.update(kw)
    return headless.PlayOpts(**base)


def _plan(mode, stream, audio_index=0, real_lang="ita", verified=True):
    return headless.stream_select.CastAudioPlan(
        mode, stream, audio_index, real_lang, verified=verified
    )


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
        "follow": None,  # BooleanOptionalAction default → fire-and-return
        "movies": False,
        "series": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _hopts(cast=False):
    return headless.PlayOpts(
        auto=True, cast=cast, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )


def _VETTED(s):
    return headless.stream_select.VettedStream(stream=s, auto=True, safety_sub_lang=None)


def _wire_movie(monkeypatch, *, name="Dune", stream=None):
    stream = stream or {
        "name": "[RD+] Torrentio\n1080p",
        "title": "Dune.2024.1080p.WEB-DL.HEVC.ITA-GRP\n👤 9 💾 8 GB",
        "url": "http://rd.example/secret-token-abc/dune.mkv",
    }
    monkeypatch.setattr(
        headless.api, "search", lambda cfg, q: [{"id": "tt1", "type": "movie", "name": name}]
    )
    monkeypatch.setattr(headless.api, "streams", lambda cfg, t, v: [stream])
    monkeypatch.setattr(
        headless.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    # Keep audio-language discovery hermetic (no real rank_streams/vainfo in unit tests).
    monkeypatch.setattr(
        headless.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    monkeypatch.setattr(headless, "auto_subs", lambda *a, **k: ())  # local-play path
    monkeypatch.setattr(cast_flow.subs, "auto_subs", lambda *a, **k: ())  # cast path
    monkeypatch.setattr(headless, "device_volume", lambda device: (0.4, False))
    return stream


def test_run_auto_movie_emits_json(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        headless, "play", lambda *a, **k: seen.update(played=True) or (0.0, 0.0, "")
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and seen.get("played")
    assert out["ok"] and out["action"] == "play" and out["type"] == "movie"
    assert out["title"] == "Dune" and out["selection"] == "exact"
    assert out["stream"]["resolution"] == 1080 and out["stream"]["codec"] == "hevc"
    assert out["stream"]["cached"] is True and out["stream"]["backend"] == "debrid"


def test_run_auto_skips_fzf(monkeypatch):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless, "play", lambda *a, **k: (0.0, 0.0, ""))

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
        headless.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(headless, "auto_subs", lambda *a, **k: ())
    monkeypatch.setattr(headless, "play", lambda *a, **k: (0.0, 0.0, ""))
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert out["imdb_id"] == "tt1" and out["selection"] == "exact"


def test_run_auto_year_disambiguates(monkeypatch, capsys):
    metas = [
        {"id": "old", "type": "movie", "name": "Dune", "releaseInfo": "1984"},
        {"id": "new", "type": "movie", "name": "Dune", "releaseInfo": "2021"},
    ]
    monkeypatch.setattr(headless.api, "search", lambda cfg, q: metas)
    monkeypatch.setattr(
        headless.api, "streams", lambda cfg, t, v: [{"name": "x", "title": "y", "url": "u"}]
    )
    monkeypatch.setattr(
        headless.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(headless, "auto_subs", lambda *a, **k: ())
    monkeypatch.setattr(headless, "play", lambda *a, **k: (0.0, 0.0, ""))
    headless.run_auto(CFG, _hns(query=["dune"], year="2021"), _hopts())
    assert json.loads(capsys.readouterr().out)["imdb_id"] == "new"


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
        headless.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(headless, "auto_subs", lambda *a, **k: ())
    seen = {}
    monkeypatch.setattr(
        headless.api,
        "streams",
        lambda cfg, t, v: seen.update(vid=v) or [{"name": "x", "title": "y", "url": "u"}],
    )
    monkeypatch.setattr(headless, "play", lambda *a, **k: (0.0, 0.0, ""))
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
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    seen = {}
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(follow=k.get("follow")) or (0.0, 0.0, False, False),
    )  # fmt: skip
    headless.run_auto(CFG, _hns(query=["dune"], device="Salotto"), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "cast" and out["device"] == "Salotto"
    assert seen["follow"] is False  # default fire-and-return


def test_run_auto_cast_remux_failure_notice(monkeypatch, capsys):
    """A failed Tier-2 remux must not degrade silently: the direct-cast fallback surfaces a
    stderr warning and the JSON `notice` (first audio track may be silent/wrong-language)."""
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    plan = headless.stream_select.CastAudioPlan("remux", stream, 1, "ita", verified=True)
    monkeypatch.setattr(headless.stream_select, "vet_cast_audio", lambda *a, **k: plan)
    monkeypatch.setattr(headless.remux, "remux_for_cast", lambda *a, **k: None)  # ffmpeg failed
    seen = {}
    monkeypatch.setattr(
        cast_flow.caster, "cast", lambda *a, **k: seen.update(cast=True) or (0.0, 0.0, False, False)
    )
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
            "url": "http://rd.example/secret-token-abc123/dune.mkv",
        },
    )
    monkeypatch.setattr(headless, "play", lambda *a, **k: (0.0, 0.0, ""))
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = capsys.readouterr().out
    assert "secret-token-abc123" not in out and "http" not in out


def test_run_auto_sub_menu_rejected(monkeypatch, capsys):
    rc = headless.run_auto(CFG, _hns(query=["dune"], sub_menu=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["error"] == "usage"


def test_run_auto_enriched_json_audio_fields(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless, "play", lambda *a, **k: (0.0, 0.0, ""))
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert out["available_audio"] == ["ita", "eng"]
    assert out["audio_lang"] == CFG.primary  # no --audio-lang → expected/primary


def test_run_auto_audio_lang_forces_dub(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    seen = {}

    def pick(cfg, results, lang, *, cast, probe_cap=4):
        seen["lang"] = lang
        return results[0], True  # (stream, verified) — track-accurate confirmed

    monkeypatch.setattr(headless.stream_select, "pick_audio_stream_verified", pick)
    # prepare_stream must NOT be used on the forced-audio path.
    monkeypatch.setattr(
        headless.stream_select, "prepare_stream",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prepare_stream used")),
    )  # fmt: skip
    monkeypatch.setattr(headless, "play", lambda *a, **k: (0.0, 0.0, ""))
    opts = headless.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, audio_lang="eng",
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"], audio_lang="eng"), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and seen["lang"] == "eng"
    assert out["audio_lang"] == "eng"


def test_run_auto_audio_lang_unavailable(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    monkeypatch.setattr(
        headless, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
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
        headless.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    monkeypatch.setattr(
        headless, "available_subtitle_langs", lambda cfg, t, v: ["eng", "fre", "ita"]
    )
    monkeypatch.setattr(
        headless, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    rc = headless.run_auto(CFG, _hns(query=["dune"], probe=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "probe"
    assert out["available_audio"] == ["ita", "eng"]
    assert out["available_subtitles"] == ["eng", "fre", "ita"]


def test_run_auto_cast_reports_volume(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: (0.0, 0.0, False, False))
    monkeypatch.setattr(headless, "device_volume", lambda device: (0.4, False))
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["volume"] == 0.4 and out["muted"] is False and out["notice"] is None


def test_run_auto_cast_warns_volume_zero(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: (0.0, 0.0, False, False))
    monkeypatch.setattr(headless, "device_volume", lambda device: (0.0, False))
    headless.run_auto(CFG, _hns(query=["dune"]), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert out["volume"] == 0.0 and out["notice"] and "volume" in out["notice"].lower()


# --- completion: stop / status / browse / volume / track-accurate ----------


def test_run_auto_stop(monkeypatch, capsys):
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless.caster, "stop", lambda device: True)
    rc = headless.run_auto(CFG, _hns(stop=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "stop" and out["device"] == "192.168.1.5"


def test_run_auto_status(monkeypatch, capsys):
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
        headless.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(
        headless.stream_select, "audio_languages", lambda cfg, results, *, cast: ("eng",)
    )
    monkeypatch.setattr(headless, "auto_subs", lambda *a, **k: ())
    monkeypatch.setattr(headless, "play", lambda *a, **k: (0.0, 0.0, ""))
    rc = headless.run_auto(CFG, _hns(browse="popolari"), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["title"] == "Popular" and out["selection"] == "browse"


def test_run_auto_cast_sets_volume(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: (0.0, 0.0, False, False))
    seen = {}
    monkeypatch.setattr(
        headless.caster, "set_volume", lambda device, level: seen.update(level=level)
    )
    monkeypatch.setattr(headless, "device_volume", lambda device: (0.35, False))
    headless.run_auto(CFG, _hns(query=["dune"], volume=35), _hopts(cast=True))
    assert seen["level"] == 35


def test_run_auto_audio_lang_not_in_real_tracks(monkeypatch, capsys):
    _wire_movie(monkeypatch)
    monkeypatch.setattr(
        headless.stream_select, "audio_languages", lambda cfg, results, *, cast: ("ita", "eng")
    )
    # name tags claim ita, but ffprobe verification finds no candidate → reject (no wrong dub).
    monkeypatch.setattr(
        headless.stream_select, "pick_audio_stream_verified",
        lambda cfg, results, lang, *, cast, probe_cap=4: (None, False),
    )  # fmt: skip
    monkeypatch.setattr(
        headless, "play", lambda *a, **k: (_ for _ in ()).throw(AssertionError("played"))
    )
    opts = headless.PlayOpts(
        auto=True, cast=False, sub_mode=None, sub_lang=None,
        history=False, autoplay=False, audio_lang="ita",
    )  # fmt: skip
    rc = headless.run_auto(CFG, _hns(query=["dune"], audio_lang="ita"), opts)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "audio_lang_unavailable"


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
        headless.stream_select, "prepare_stream",
        lambda cfg, results, opts, *, auto, reselect_on_wrong_audio: _VETTED(results[0]),
    )  # fmt: skip
    monkeypatch.setattr(headless, "auto_subs", lambda *a, **k: ())
    monkeypatch.setattr(headless, "play", lambda *a, **k: (0.0, 0.0, ""))


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


# --- headless cast tree (_auto_play) — safety net for the headless extraction ---


def test_run_auto_mirror_action(monkeypatch, capsys):
    """Headless twin of the mirror gate: --json --cast --mirror + plan remux → action=mirror."""
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        headless.stream_select,
        "vet_cast_audio",
        lambda *a, **k: _plan("remux", stream, audio_index=1),
    )
    monkeypatch.setattr(headless.mirror, "available", lambda: True)
    seen = {}
    monkeypatch.setattr(
        headless.mirror, "cast_via_mirror",
        lambda cfg, title, url, **k: (
            seen.update(url=url, follow=k.get("follow")) or (0.0, 0.0, False)
        ),
    )  # fmt: skip
    monkeypatch.setattr(headless.remux, "remux_for_cast", _boom("mirror must preempt the remux"))
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
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        headless.stream_select,
        "vet_cast_audio",
        lambda *a, **k: _plan("remux", stream, audio_index=1),
    )
    monkeypatch.setattr(headless.remux, "remux_for_cast", lambda *a, **k: "/tmp/out.mp4")
    seen = {"detached": 0}
    monkeypatch.setattr(
        headless.remux, "cast_file",
        lambda cfg, title, path, **k: (
            seen.update(path=path, follow=k.get("follow"), on_event=k.get("on_event"))
            or (0.0, 0.0, False, False)
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
        lambda cfg, typ, vid, wd, opts, safety_sub_lang=None: (
            calls.append(safety_sub_lang) or ("/tmp/sub.srt",)
        ),
    )  # fmt: skip
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        headless.stream_select,
        "vet_cast_audio",
        lambda *a, **k: _plan("absent", stream, real_lang="eng"),
    )
    monkeypatch.setattr(headless.remux, "remux_for_cast", _boom("no remux for an absent language"))
    seen = {}
    monkeypatch.setattr(
        cast_flow.caster, "cast", lambda *a, **k: seen.update(cast=True) or (0.0, 0.0, False, True)
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
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        headless.stream_select, "vet_cast_audio", lambda *a, **k: _plan("direct", stream)
    )
    seen = {}

    def fake_cast(cfg, title, url, **k):
        seen["follow"] = k.get("follow")
        k["on_event"]({"kind": "playing", "position": 3.0})  # the callback IS the JSONL writer
        return (0.0, 0.0, False, False)

    monkeypatch.setattr(cast_flow.caster, "cast", fake_cast)
    rc = headless.run_auto(CFG, _hns(query=["dune"], follow=True), _cast_opts())
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 0 and seen["follow"] is True
    event, final = lines[0], lines[-1]
    assert event["event"] == "playing" and event["ok"] is True and event["position"] == 3.0
    assert final["action"] == "cast" and final["error"] is None


# --- resume / headless-continue perimeter — safety net for the headless extraction ---


def test_run_auto_resume_threads_history_entry(monkeypatch):
    """--json -c with a query: the matched series entry's identity reaches _auto_play."""
    entry = HistoryEntry(
        video_id="tt2:1:2", title="Severance", type="series", series_id="tt2",
        season=1, episode=2, position=100.0, duration=3000.0, ts=1.0,
    )  # fmt: skip
    monkeypatch.setattr(headless.state, "recent", lambda cfg, limit=30, typ=None: [entry])
    seen = {}

    def spy(cfg, args, opts, typ, video_id, title, imdb_id, season, episode, selection, **kw):
        seen.update(
            typ=typ, video_id=video_id, title=title, imdb_id=imdb_id,
            season=season, episode=episode, selection=selection,
        )  # fmt: skip
        return 0

    monkeypatch.setattr(headless, "_auto_play", spy)
    rc = headless.run_auto(CFG, _hns(query=["severance"], cont=True), _hopts())
    assert rc == 0
    assert seen["typ"] == "series" and seen["video_id"] == "tt2:1:2"
    assert seen["imdb_id"] == "tt2" and seen["season"] == 1 and seen["episode"] == 2
    assert seen["selection"] == "resume" and seen["title"] == "Severance · S01E02"


def test_run_auto_resume_no_result(monkeypatch, capsys):
    """--json -c: empty history and a no-match query are both no_result (rc 1)."""
    monkeypatch.setattr(headless, "_auto_play", _boom("nothing should play"))
    monkeypatch.setattr(headless.state, "recent", lambda cfg, limit=30, typ=None: [])
    rc = headless.run_auto(CFG, _hns(cont=True), _hopts())
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["error"] == "no_result" and "cronologia vuota" in out["message"]
    entry = HistoryEntry(video_id="tt3", title="Dune", type="movie", ts=1.0)
    monkeypatch.setattr(headless.state, "recent", lambda cfg, limit=30, typ=None: [entry])
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
    monkeypatch.setattr(headless, "play", lambda *a, **k: (600.0, 6000.0, ""))
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hist_opts(cast=False))
    assert rc == 0
    e = headless.state.load_history(CFG)["tt1"]
    assert (e["position"], e["duration"], e["title"]) == (600.0, 6000.0, "Dune")


def test_follow_cast_saves_history(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: (600.0, 6000.0, False, False))
    rc = headless.run_auto(CFG, _hns(query=["dune"], follow=True), _hist_opts(cast=True))
    assert rc == 0
    e = headless.state.load_history(CFG)["tt1"]
    assert (e["position"], e["duration"]) == (600.0, 6000.0)


def test_fire_and_return_notes_started_and_session(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    _wire_movie(monkeypatch)
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: (0.0, 0.0, False, False))
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _hist_opts(cast=True))
    assert rc == 0
    # started entry: the title is now known to -c (no more restart-at-S01E01)
    e = headless.state.load_history(CFG)["tt1"]
    assert (e["position"], e["duration"], e["title"]) == (0.0, 0.0, "Dune")
    # cast session: --stop/--status can attribute the receiver position to it
    session = headless.state.util.RunState(headless.state.CAST_SESSION).read()
    assert session is not None
    assert session["video_id"] == "tt1" and session["device"] == "192.168.1.5"


def test_stop_persists_receiver_position_and_counts_remux(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    headless.state.remember_cast(
        CFG, headless.state.make_entry("tt9", "Dune", "movie", 0.0, 0.0), "192.168.1.5"
    )
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(headless.mirror, "stop", lambda: False)
    monkeypatch.setattr(
        headless.caster, "status",
        lambda device: {"player_state": "PLAYING", "title": "Dune", "position": 1000.0,
                        "duration": 5000.0, "volume": 0.4, "muted": False},
    )  # fmt: skip
    monkeypatch.setattr(headless.caster, "stop", lambda device: False)  # TV unreachable…
    monkeypatch.setattr(headless.remux, "stop", lambda device: True)  # …but remux reclaimed
    rc = headless.run_auto(CFG, _hns(stop=True), _hopts(cast=True))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True  # remux.stop success counts (L5)
    e = headless.state.load_history(CFG)["tt9"]
    assert (e["position"], e["duration"]) == (1000.0, 5000.0)
    assert headless.state.util.RunState(headless.state.CAST_SESSION).read() is None  # one-shot


def test_status_refreshes_session_entry(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    headless.state.remember_cast(
        CFG, headless.state.make_entry("tt9", "Dune", "movie", 0.0, 0.0), "192.168.1.5"
    )
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
    assert headless.state.util.RunState(headless.state.CAST_SESSION).read() is not None


def test_subtitles_not_reported_when_delivery_drops_them(monkeypatch, capsys):
    """M3: subs the castbridge LOAD can't carry must not be claimed in the JSON."""
    stream = _wire_movie(monkeypatch)
    monkeypatch.setattr(cast_flow.subs, "auto_subs", lambda *a, **k: ("/tmp/sub.srt",))
    monkeypatch.setattr(headless, "_resolve_device", lambda cfg, **k: "192.168.1.5")
    monkeypatch.setattr(
        headless.stream_select,
        "vet_cast_audio",
        lambda *a, **k: _plan("absent", stream, real_lang="eng"),
    )
    monkeypatch.setattr(headless.remux, "remux_for_cast", _boom("no remux here"))
    # bridge path: subs_delivered=False
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: (0.0, 0.0, False, False))
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    rc = headless.run_auto(CFG, _hns(query=["dune"]), _cast_opts())
    cap = capsys.readouterr()
    out = json.loads(cap.out)
    assert rc == 0 and out["subtitles"] is None
    assert "sottotitoli non supportati" in cap.err  # honesty notice on stderr
