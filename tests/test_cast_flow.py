"""Unit tests for the shared cast decision tree (`cast_flow.run_cast`).

These exercise the helper directly (the wrapper wiring — `cli._play_on_cast` and the
headless `--json --cast` branch — stays covered in test_cli.py). Patch seams are the
module attributes cast_flow re-imports: `cast_flow.cast_vet` / `remux` / `mirror` /
`caster` / `subs` / `engine`."""

from __future__ import annotations

import sys
from dataclasses import replace

import pytest

from nstream import cast_delivery, cast_flow, subs, util
from nstream.config import Config, PlayOpts
from nstream.types import Stream

CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])

_STREAM: Stream = {
    "name": "[RD+] Torrentio\n1080p",
    "title": "Dune.2024.1080p.WEB-DL.HEVC.ITA-GRP\n👤 9 💾 8 GB",
    "url": "http://u/dune.mkv",
}


def _opts(**kw):
    return replace(
        PlayOpts(auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False),
        **kw,
    )


def _plan(mode, stream, audio_index=0, real_lang="ita", verified=True, needs_remux=False):
    return cast_flow.cast_vet.CastAudioPlan(
        mode, stream, audio_index, real_lang, verified=verified, needs_remux=needs_remux
    )


def _ok(pos: float = 0.0, dur: float = 0.0, subs: bool = False):
    """A delivery stub that really delivered. Backend stubs must return a CastResult, not a
    bare tuple: a plain tuple keeps satisfying the old unpacking and stays green while
    production breaks — the stub-drift trap ADR 0031 names."""
    return cast_delivery.CastResult(pos, dur, subs, started=True)


def _boom(msg):
    def fail(*a, **k):
        raise AssertionError(msg)

    return fail


def _wire(monkeypatch, plan, *, langs=("ita",)):
    """Hermetic run_cast: stub vet_cast_video/vet_cast_audio (the real ones ffprobe the
    url), the in-cast switch helpers, auto_subs and detach_spawned. Returns the spy dict."""
    seen: dict = {"subs": [], "detached": 0}
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_video",
        lambda cfg, results, chosen, exact_resolution=0, **_kw: (chosen, ""),
    )
    # Container vetting (ADR 0022): default to a castable container so the existing tests take
    # the direct/remux paths they assert. The reselect returns (chosen, False) and the settled
    # guarantee (`quality.container_castable(cast_container(...))`) sees mp4. Container tests override.
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_container",
        lambda cfg, results, chosen, target, exact_resolution=0, **_kw: (chosen, False),
    )
    monkeypatch.setattr(cast_flow.cast_vet, "cast_container", lambda cfg, stream: "mp4")
    monkeypatch.setattr(cast_flow.cast_vet, "vet_cast_audio", lambda *a, **k: plan)
    # ADR 0035 stays opt-in per test: the real search ffprobes URLs.
    monkeypatch.setattr(cast_flow.cast_vet, "find_instant_direct", lambda *a, **k: None)
    monkeypatch.setattr(
        cast_flow.cast_vet, "cast_languages",
        lambda cfg, results, exact_resolution=0, **_kw: langs,
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "cast_resolver",
        lambda cfg, results, exact_resolution=0, **_kw: lambda lang: "http://u2",
    )
    monkeypatch.setattr(
        cast_flow.subs, "auto_subs",
        lambda cfg, typ, vid, wd, opts, safety_sub_lang=None, **kw: (
            seen["subs"].append(safety_sub_lang) or subs.SubsPick()
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.engine, "detach_spawned",
        lambda: seen.update(detached=seen["detached"] + 1),
    )  # fmt: skip
    return seen


def _run(opts, stream, *, start=None, results=None, **kw):
    return cast_flow.run_cast(
        CFG, results if results is not None else [stream], stream,
        device="192.168.1.5", title="Dune", typ="movie", video_id="tt1",
        work_dir="/tmp", opts=opts, start=start, **kw,
    )  # fmt: skip


# --- Tier-2 remux ------------------------------------------------------------


def test_remux_success_uses_cast_file(monkeypatch):
    """Tier-2 plan + remux OK → cast_file with start/follow/on_event threaded; the direct
    cast never runs and the outcome reports reencoded."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(
        cast_flow.remux, "remux_for_cast",
        lambda url, cfg, *, audio_index, size_gb=0.0: (
            seen.update(remux_url=url, idx=audio_index) or "/tmp/out.mp4"
        ),
    )  # fmt: skip
    cb = object()
    monkeypatch.setattr(
        cast_flow.remux, "cast_file",
        lambda cfg, title, path, **k: (
            seen.update(
                path=path, follow=k.get("follow"), start=k.get("start"),
                on_event=k.get("on_event"),
            )
            or _ok()
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.caster, "cast", _boom("direct cast must not run when the remux succeeds")
    )
    out = _run(_opts(), stream, start=42.0, follow=False, on_event=cb)
    assert seen["remux_url"] == stream["url"] and seen["idx"] == 1  # plan's track is mapped
    assert seen["path"] == "/tmp/out.mp4"
    assert seen["follow"] is False and seen["start"] == 42.0 and seen["on_event"] is cb
    assert out.reencoded is True and out.action == "cast" and out.notice is None


def test_remux_failure_degrades_to_direct(monkeypatch, capsys):
    """remux refused/failed → stderr warns and the direct cast carries the ORIGINAL url;
    the warning is also returned as the outcome notice (the --json field)."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", lambda *a, **k: None)  # ffmpeg failed
    monkeypatch.setattr(
        cast_flow.remux, "cast_file", _boom("cast_file must not run without a remux")
    )
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(cast_url=a[2]) or _ok(),
    )  # fmt: skip
    out = _run(_opts(), stream)
    assert "remux non riuscito" in capsys.readouterr().err
    assert seen["cast_url"] == stream["url"]
    assert out.notice and "remux non riuscito" in out.notice
    assert out.reencoded is False and out.action == "cast"


def test_headless_prefers_direct_over_remux(monkeypatch, capsys):
    """Soft language preference: a verified English MP4 starts now; Italian waits."""
    slow: Stream = _STREAM.copy()
    eng: Stream = {"url": "http://u/eng.mp4", "name": "Film.2015.1080p.AAC.mp4"}
    seen = _wire(monkeypatch, _plan("remux", slow, real_lang="ita"))
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "find_instant_direct",
        lambda *a, **k: _plan("direct", eng, real_lang="eng"),
    )
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("direct cast must skip the remux"))
    monkeypatch.setattr(
        cast_flow.caster,
        "cast",
        lambda *a, **k: seen.update(url=a[2]) or _ok(),
    )
    out = _run(_opts(), slow, follow=False)
    assert seen["url"] == eng["url"]
    assert seen["subs"] == ["ita"]
    assert out.audio_lang == "eng" and out.reencoded is False
    assert out.notice and "cast diretto eng" in out.notice
    assert "cast diretto eng" in capsys.readouterr().err


def test_tty_can_keep_the_remux(monkeypatch):
    """A TUI that declines the instant dub still prepares the primary-language file."""
    stream: Stream = _STREAM.copy()
    eng: Stream = {"url": "http://u/eng.mp4"}
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "find_instant_direct",
        lambda *a, **k: _plan("direct", eng, real_lang="eng"),
    )
    monkeypatch.setattr(cast_flow.picker, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(
        cast_flow.remux,
        "remux_for_cast",
        lambda url, cfg, *, audio_index, size_gb=0.0: seen.update(remux=True) or "/tmp/out.mp4",
    )
    monkeypatch.setattr(cast_flow.remux, "cast_file", lambda *a, **k: _ok())
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("declined instant cast must remux"))
    out = _run(_opts(), stream)
    assert seen["remux"] is True and out.reencoded is True and out.notice is None


def test_explicit_audio_lang_does_not_search_instant(monkeypatch):
    """`--audio-lang` keeps the full remux even when a direct release exists."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("remux", stream))
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "find_instant_direct",
        lambda *a, **k: seen.update(searched=True),
    )
    monkeypatch.setattr(
        cast_flow.remux,
        "remux_for_cast",
        lambda url, cfg, *, audio_index, size_gb=0.0: "/tmp/out.mp4",
    )
    monkeypatch.setattr(cast_flow.remux, "cast_file", lambda *a, **k: _ok())
    _run(_opts(audio_lang="ita"), stream)
    assert "searched" not in seen


# --- absent dub (normalizations 1 + 2) ---------------------------------------


def test_absent_safety_subs_once_with_notice(monkeypatch, capsys):
    """No dub carries the primary language → auto_subs runs exactly ONCE with the safety
    language, stderr explains (on every path), and the pick is cast directly anyway."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("absent", stream, real_lang="eng"))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("no remux for an absent language"))
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(cast_url=a[2]) or _ok(),
    )  # fmt: skip
    out = _run(_opts(), stream)
    err = capsys.readouterr().err
    assert seen["subs"] == [CFG.primary]  # ONE call, safety lang set (normalization 2)
    # The audio fact is stated; the subtitle line is NOT, because `auto_subs` (stubbed)
    # delivered nothing. This assertion used to demand the opposite — it encoded the bug
    # where the promise was printed at the decision site, before any fetch had run.
    assert "non disponibile" in err
    assert f"sottotitoli {CFG.primary} attivati" not in err
    assert seen["cast_url"] == stream["url"]
    assert out.safety_sub_lang == CFG.primary
    assert out.audio_lang == "eng" and out.audio_verified is True


def test_absent_dolby_still_remuxes_with_safety_subs(monkeypatch, capsys):
    """Root-cause regression: target language absent AND the fallback dub's default track is
    Dolby (`needs_remux`). The remux must still run (else silent), while the safety subs and
    the 'non disponibile' notice of the absent path are preserved — the two axes coexist."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("absent", stream, real_lang="eng", needs_remux=True))
    monkeypatch.setattr(
        cast_flow.remux, "remux_for_cast",
        lambda url, cfg, *, audio_index, size_gb=0.0: (
            seen.update(remux_url=url, idx=audio_index) or "/tmp/out.mp4"
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.remux,
        "cast_file",
        lambda cfg, title, path, **k: seen.update(path=path) or _ok(),
    )
    monkeypatch.setattr(
        cast_flow.caster, "cast", _boom("direct cast must not run for an absent Dolby fallback")
    )
    out = _run(_opts(), stream)
    err = capsys.readouterr().err
    assert seen["path"] == "/tmp/out.mp4" and seen["remux_url"] == stream["url"]
    assert seen["subs"] == [CFG.primary] and "non disponibile" in err  # absent path preserved
    assert out.reencoded is True and out.safety_sub_lang == CFG.primary


def test_safety_sub_lang_passthrough(monkeypatch):
    """A caller-provided safety language (from prepare_stream's vet) reaches auto_subs and
    the outcome untouched when the cast plan isn't `absent`."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    out = _run(_opts(), stream, safety_sub_lang="ita")
    assert seen["subs"] == ["ita"]
    assert out.safety_sub_lang == "ita"


def test_explicit_sub_lang_overrides_absent_safety(monkeypatch, capsys):
    """`--sub-lang` set + target audio absent: the explicit subtitle language wins over the
    primary-language safety default (auto_subs gets no safety lang), and it's threaded to the
    cast as the caption-track language. The audio-absent warning still prints."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("absent", stream, real_lang="eng"))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("no remux on this path"))
    captured: dict = {}
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: captured.update(sub_lang=k.get("sub_lang")) or _ok(subs=True),
    )  # fmt: skip
    out = _run(_opts(sub_mode="auto", sub_lang="eng"), stream)
    err = capsys.readouterr().err
    assert seen["subs"] == [None]  # NO safety override — explicit --sub-lang wins
    assert captured["sub_lang"] == "eng"  # explicit language threaded to the cast track
    assert "non disponibile" in err and "attivati" not in err
    assert out.safety_sub_lang is None and out.subs_delivered is True


def test_bridge_subtitles_reported_delivered(monkeypatch):
    """The bridge cast now carries subtitles: when the delivery reports subs_delivered=True,
    the outcome reflects it (no 'not loaded' honesty notice) and threads the safety language."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("absent", stream, real_lang="eng"))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("direct cast, no remux"))
    captured: dict = {}
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: captured.update(sub_lang=k.get("sub_lang")) or _ok(subs=True),
    )  # fmt: skip
    out = _run(_opts(), stream)
    assert seen["subs"] == [CFG.primary]  # safety subs fetched (no explicit --sub-lang)
    assert captured["sub_lang"] == CFG.primary  # safety language labels the track
    assert out.subs_delivered is True and out.safety_sub_lang == CFG.primary


# --- mirror gate --------------------------------------------------------------


def test_mirror_gates_on_remux_audio(monkeypatch):
    """--mirror + undecodable audio (plan remux) → cast_via_mirror with follow threaded;
    remux and the direct cast must not run."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror",
        lambda cfg, title, url, **k: (
            seen.update(
                url=url, device=k.get("device"), start=k.get("start"), follow=k.get("follow")
            )
            or _ok()
        ),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("mirror must preempt the remux"))
    monkeypatch.setattr(
        cast_flow.caster, "cast", _boom("direct cast must not run on the mirror path")
    )
    out = _run(_opts(mirror=True), stream, start=7.0, follow=False)
    assert seen["url"] == stream["url"]
    assert seen["device"] == "192.168.1.5" and seen["start"] == 7.0
    assert seen["follow"] is False  # headless fire-and-return reaches the mirror too
    assert out.action == "mirror" and out.reencoded is False


def test_explicit_mirror_forces_mirror_when_available(monkeypatch):
    """--mirror with DMR-decodable audio now FORCES the mirror (ADR 0023): the manual intent
    wins even for a title the DMR could play directly."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror",
        lambda *a, **k: seen.update(mirror=True) or _ok(),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("direct cast must not run when forced"))
    out = _run(_opts(mirror=True), stream)
    assert seen.get("mirror") is True and out.action == "mirror"


def test_explicit_mirror_falls_back_when_unavailable(monkeypatch, capsys):
    """--mirror but the mirror backend is unavailable → honest fallback notice + direct cast."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: False)
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: seen.update(cast=True) or _ok())
    out = _run(_opts(mirror=True), stream)
    assert cast_flow.MIRROR_UNAVAILABLE in capsys.readouterr().err
    assert seen.get("cast") is True and out.action == "cast"


# --- auto mirror over a pathological 4K remux (ADR 0015) ----------------------

# A 4K Dolby-only release: undecodable audio (needs_remux) + a huge fetch.
_STREAM_4K: Stream = {
    "name": "[RD+] Torrentio\n2160p",
    "title": "Dune.2024.2160p.UHD.BluRay.REMUX.TrueHD\n👤 12 💾 55 GB",
    "url": "http://u/dune-4k.mkv",
}


def _wire_mirror(monkeypatch, seen):
    """Route the mirror path to a spy and boom the remux + direct cast (must be preempted)."""
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror",
        lambda cfg, title, url, **k: seen.update(mirror=url) or _ok(),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("mirror must preempt the remux"))
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("direct cast must not run on the mirror"))


def test_auto_mirror_on_pathological_4k_remux(monkeypatch, capsys):
    """No --mirror, but a 4K Dolby-only remux (55 GB ≥ threshold) with the mirror available →
    auto-prefer the mirror: it preempts the remux and the outcome carries the degrade notice."""
    stream: Stream = _STREAM_4K.copy()
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    _wire_mirror(monkeypatch, seen)
    out = _run(_opts(mirror=None), stream)  # NOT forced — the size threshold drives it
    assert seen["mirror"] == stream["url"]
    assert out.action == "mirror" and out.reencoded is False
    assert out.notice and "mirror 1080p" in out.notice
    assert "mirror 1080p" in capsys.readouterr().err


def test_no_auto_mirror_for_small_1080p_remux(monkeypatch):
    """A modest 1080p Dolby remux (8 GB < threshold) → the remux still wins (native video),
    the mirror is not auto-chosen even though it's available."""
    stream: Stream = _STREAM.copy()  # 1080p, 8 GB
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror", _boom("small remux must not auto-mirror")
    )
    monkeypatch.setattr(
        cast_flow.remux, "remux_for_cast", lambda url, cfg, **k: "/tmp/out.mp4"
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.remux, "cast_file",
        lambda *a, **k: seen.update(remuxed=True) or _ok(),
    )  # fmt: skip
    out = _run(_opts(mirror=None), stream)
    assert seen.get("remuxed") is True
    assert out.action == "cast" and out.reencoded is True and out.notice is None


def test_auto_mirror_disabled_by_zero_threshold(monkeypatch):
    """cast_mirror_over_remux_gb=0 disables the auto-switch: even a 55 GB 4K remux takes the
    remux path (the config opt-out), the mirror is never auto-chosen."""
    cfg = Config(torrentio_base="tb", cast_mirror_over_remux_gb=0)
    stream: Stream = _STREAM_4K.copy()
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror", _boom("zero threshold must not auto-mirror")
    )
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", lambda url, cfg, **k: "/tmp/out.mp4")
    monkeypatch.setattr(
        cast_flow.remux, "cast_file",
        lambda *a, **k: seen.update(remuxed=True) or _ok(),
    )  # fmt: skip
    out = cast_flow.run_cast(
        cfg, [stream], stream, device="192.168.1.5", title="Dune", typ="movie",
        video_id="tt1", work_dir="/tmp", opts=_opts(mirror=None), start=None,
    )  # fmt: skip
    assert seen.get("remuxed") is True and out.action == "cast"


def test_remux_pathological_helper():
    """Size ≥ threshold, or 4K with unknown size, is pathological; small or unknown-1080p not."""
    from nstream.quality import StreamInfo

    assert cast_flow._remux_is_pathological(StreamInfo(size_gb=55.0), 10) is True
    assert cast_flow._remux_is_pathological(StreamInfo(size_gb=8.0), 10) is False
    assert cast_flow._remux_is_pathological(StreamInfo(resolution=2160), 10) is True  # size unknown
    assert cast_flow._remux_is_pathological(StreamInfo(resolution=1080), 10) is False
    assert cast_flow._remux_is_pathological(StreamInfo(size_gb=55.0), 0) is False  # disabled


# --- cast container gate (ADR 0022) ------------------------------------------


def test_bad_container_triggers_rewrap(monkeypatch):
    """A DMR-incompatible container (mkv) with decodable audio still routes through the Tier-2
    rewrap to MP4 — the direct cast (which the DMR would refuse) never runs."""
    stream: Stream = _STREAM.copy()  # 1080p, 8 GB → rewrap (not pathological), not the mirror
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.cast_vet, "cast_container", lambda cfg, s: "mkv")
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: False)
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", lambda url, cfg, **k: "/tmp/out.mp4")
    monkeypatch.setattr(cast_flow.subs, "align_local", lambda cfg, pick, path, wd, opts: pick)
    monkeypatch.setattr(
        cast_flow.remux, "cast_file",
        lambda *a, **k: seen.update(remuxed=True) or _ok(),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("bad container must not direct-cast"))
    out = _run(_opts(), stream)
    assert seen.get("remuxed") is True and out.reencoded is True and out.action == "cast"


def test_bad_container_4k_prefers_mirror(monkeypatch):
    """A 4K MKV with no MP4 twin and decodable audio: the rewrap would be a huge fetch, so the
    pathological-4K auto-switch (ADR 0015) prefers the mirror over the container rewrap."""
    stream: Stream = _STREAM_4K.copy()  # 2160p, 55 GB
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.cast_vet, "cast_container", lambda cfg, s: "mkv")
    _wire_mirror(monkeypatch, seen)
    out = _run(_opts(mirror=None), stream)
    assert seen["mirror"] == stream["url"] and out.action == "mirror"


def test_bad_container_mirrors_when_rewrap_unavailable(monkeypatch, capsys):
    """ADR 0022 gap: a bad-container pick with the rewrap unavailable (cast_remux off / ffmpeg
    missing → remux_for_cast None) mirrors instead of a silent black .mkv direct cast."""
    stream: Stream = _STREAM.copy()  # 1080p, not pathological
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.cast_vet, "cast_container", lambda cfg, s: "mkv")
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.remux, "remux_for_cast", lambda *a, **k: None
    )  # rewrap off/failed
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror",
        lambda *a, **k: seen.update(mirror=True) or _ok(),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("must not direct-cast an unsupported .mkv"))
    out = _run(_opts(), stream)
    assert seen.get("mirror") is True and out.action == "mirror"
    assert "rewrap non disponibile" in capsys.readouterr().err


def test_direct_cast_declares_container_mime(monkeypatch):
    """A castable-container direct cast declares the container MIME on the LOAD instead of
    leaving the DMR to sniff (ADR 0022)."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.cast_vet, "cast_container", lambda cfg, s: "mp4")
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(mime=k["meta"].content_type) or _ok(),
    )  # fmt: skip
    _run(_opts(), stream)
    assert seen.get("mime") == "video/mp4"


# --- in-cast audio switch wiring ----------------------------------------------


def test_lang_switch_wired_only_when_allowed(monkeypatch):
    """allow_lang_switch=True with several dubs → langs + resolver reach the cast; with a
    single language both stay empty; headless (False) never pays the extra rank pass."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("direct", stream), langs=("ita", "eng"))
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: (
            seen.update(langs=k.get("langs"), resolver=k.get("resolve_lang")) or _ok()
        ),
    )  # fmt: skip
    _run(_opts(), stream, allow_lang_switch=True)
    assert seen["langs"] == ("ita", "eng") and callable(seen["resolver"])
    monkeypatch.setattr(
        cast_flow.cast_vet, "cast_languages",
        lambda cfg, results, exact_resolution=0, **_kw: ("ita",),
    )  # fmt: skip
    _run(_opts(), stream, allow_lang_switch=True)
    assert seen["langs"] == () and seen["resolver"] is None
    monkeypatch.setattr(
        cast_flow.cast_vet, "cast_languages", _boom("headless must skip the rank pass")
    )
    _run(_opts(), stream)  # allow_lang_switch defaults to False
    assert seen["langs"] == () and seen["resolver"] is None


def test_meta_threaded_and_backend_never_sees_next_label(monkeypatch):
    """meta is threaded with its fields preserved and the container contentType added (ADR
    0022 augments meta, it does not replace its content). A delivery backend must NOT receive
    `next_label`: the advance decision is `cast_flow`'s alone (ADR 0029)."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("direct", stream))  # cast_container stubbed → mp4
    meta = cast_flow.caster.CastMeta(poster="http://img/p.jpg")
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: (
            seen.update(next_label=k.get("next_label"), meta=k.get("meta"))
            or _ok(1.0, 2.0)
        ),
    )  # fmt: skip
    out = _run(_opts(), stream, next_label="S01E02", meta=meta)
    assert seen["next_label"] is None  # backends never learn that an episode follows
    assert seen["meta"].poster == "http://img/p.jpg"  # original field preserved
    assert seen["meta"].content_type == "video/mp4"  # container MIME declared on the LOAD
    # 1.0 of 2.0 is half the episode: reported position, no advance.
    assert (out.pos, out.dur, out.advance) == (1.0, 2.0, False)


# --- fire-and-return handoff ---------------------------------------------------


@pytest.mark.parametrize("follow,expected", [(True, 0), (False, 1)])
def test_detach_spawned_gated_on_follow(monkeypatch, follow, expected):
    """The TorrServer handoff (engine.detach_spawned) runs inside the helper, only on
    fire-and-return (follow=False) — interactive follow keeps ownership."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    _run(_opts(), stream, follow=follow)
    assert seen["detached"] == expected


def test_run_cast_clears_previous_session(monkeypatch, tmp_path):
    """A new cast replaces the TV's content: the previous fire-and-return session must
    not survive to swallow the new content's position (cold review #4)."""
    from nstream import state

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    state.remember_cast(
        Config(torrentio_base="tb"),
        state.make_entry("tt-old", "Old", "movie", 0.0, 0.0),
        "192.168.1.9",
    )
    stream: Stream = {"url": "http://u", "name": "S", "title": "T"}
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    cast_flow.run_cast(
        Config(torrentio_base="tb"), [stream], stream,
        device="192.168.1.9", title="T", typ="movie", video_id="tt1",
        work_dir=str(tmp_path), opts=_opts(), start=None, follow=False,
    )  # fmt: skip
    assert util.RunState(state.CAST_SESSION).read() is None
    assert seen is not None  # wiring sanity


# --- video-codec vetting (ADR 0017) ------------------------------------------


def test_run_cast_raises_when_video_unsupported_and_no_mirror(monkeypatch):
    """No castable video anywhere and no mirror: run_cast must fail explicitly BEFORE any
    side effect (session clear, subs fetch) — casting would play black with state PLAYING
    and no receiver error (the Coherence DivX incident, 2026-07-16)."""
    stream: Stream = _STREAM.copy()
    _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_video",
        lambda cfg, results, chosen, exact_resolution=0, **_kw: (chosen, "mpeg4"),
    )
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: False)
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("must not cast undecodable video"))
    monkeypatch.setattr(
        cast_flow.state, "clear_cast_session", _boom("failure must be side-effect free")
    )
    with pytest.raises(cast_flow.CastVideoUnsupported) as exc:
        _run(_opts(), stream)
    assert exc.value.codec == "mpeg4"


def test_run_cast_mirrors_when_video_unsupported(monkeypatch):
    """Undecodable video + mirror available → forced mirror (mpv decodes locally), even
    though the audio alone would have allowed a direct cast; the notice says why."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_video",
        lambda cfg, results, chosen, exact_resolution=0, **_kw: (chosen, "mpeg4"),
    )
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror",
        lambda cfg, title, url, **k: seen.update(mirrored=url) or _ok(),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.caster, "cast", _boom("direct cast must not run on undecodable video")
    )
    out = _run(_opts(), stream)
    assert seen["mirrored"] == stream["url"]
    assert out.action == "mirror" and "MPEG4" in (out.notice or "")


def test_run_cast_video_verdict_cleared_by_audio_reselect(monkeypatch):
    """vet_cast_audio swapping the stream clears the bad-video verdict: the language
    reselect only offers video-castable candidates, so the swap must cast directly
    instead of forcing the mirror for the abandoned stream's codec."""
    stream: Stream = _STREAM.copy()
    good = {"name": "S", "title": "T", "url": "http://u/good.mkv"}
    seen = _wire(monkeypatch, _plan("direct", good))  # audio vet swaps to `good`
    monkeypatch.setattr(
        cast_flow.cast_vet,
        "vet_cast_video",
        lambda cfg, results, chosen, exact_resolution=0, **_kw: (chosen, "mpeg4"),
    )
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror", _boom("swap resolved the video → no mirror")
    )
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(cast_url=a[2]) or _ok(),
    )  # fmt: skip
    out = _run(_opts(), stream)
    assert seen["cast_url"] == good["url"] and out.action == "cast"


# --- local-media subtitle alignment wiring (ADR 0020) -------------------------


def test_remux_path_runs_align_local_on_remux_output(monkeypatch):
    """Tier 2 of the subtitle pipeline runs against the REMUX OUTPUT (the file the
    receiver plays), after the remux and before the VTT is built: the aligned paths
    must be what cast_file serves, and the outcome must carry match/offset."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(
        cast_flow.remux,
        "remux_for_cast",
        lambda url, cfg, *, audio_index, size_gb=0.0: "/tmp/out.mp4",
    )
    aligned = subs.SubsPick(("/tmp/ita-aligned.srt",), "audio", offset_s=-7.5)
    calls = []
    monkeypatch.setattr(
        cast_flow.subs, "align_local",
        lambda cfg, pick, media, wd, opts: calls.append((pick.match, media)) or aligned,
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.remux, "cast_file",
        lambda cfg, title, path, **k: (
            seen.update(served_subs=k.get("sub_paths")) or _ok(subs=True)
        ),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("direct cast must not run"))
    out = _run(_opts(), stream)
    assert calls == [(None, "/tmp/out.mp4")]  # invoked once, on the remux output
    assert seen["served_subs"] == ("/tmp/ita-aligned.srt",)  # the ALIGNED file is served
    assert out.sub_match == "audio" and out.sub_offset == -7.5


def test_direct_path_never_runs_align_local(monkeypatch):
    """No remux → no local media → tier 2 must not run (honest lang delivery)."""
    stream: Stream = _STREAM.copy()
    _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(
        cast_flow.subs, "align_local", _boom("align_local must not run without a local file")
    )
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok(subs=True))
    out = _run(_opts(), stream)
    assert out.sub_match is None and out.sub_offset is None


# --- per-invocation constraint parity + mirror tri-state (ADR 0021) -----------


def test_run_cast_threads_resolved_quality_to_all_reselects(monkeypatch):
    """run_cast derives exact from opts.quality (the boundary replace() guarantees it is
    resolved) and hands it to video vet, audio vet and the in-cast switch helpers."""
    stream: Stream = _STREAM.copy()
    seen = {}
    monkeypatch.setattr(
        cast_flow.cast_vet, "vet_cast_video",
        lambda cfg, results, chosen, exact_resolution=0, **_kw: (
            seen.update(video=exact_resolution) or (chosen, "")
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.cast_vet, "vet_cast_container",
        lambda cfg, results, chosen, target, exact_resolution=0, **_kw: (
            seen.update(container=exact_resolution) or (chosen, False)
        ),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.cast_vet, "cast_container", lambda cfg, s: "mp4")
    monkeypatch.setattr(
        cast_flow.cast_vet, "vet_cast_audio",
        lambda cfg, results, chosen, lang, exact_resolution=0, **_kw: (
            seen.update(audio=exact_resolution) or _plan("direct", stream)
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.cast_vet, "cast_languages",
        lambda cfg, results, exact_resolution=0, **_kw: (
            seen.update(langs=exact_resolution) or ("ita", "eng")
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.cast_vet, "cast_resolver",
        lambda cfg, results, exact_resolution=0, **_kw: (
            seen.update(resolver=exact_resolution) or (lambda lang: None)
        ),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.subs, "auto_subs", lambda *a, **k: subs.SubsPick())
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    cast_flow.run_cast(
        CFG, [stream], stream,
        device="192.168.1.5", title="T", typ="movie", video_id="tt1",
        work_dir="/tmp", opts=_opts(quality=1080), start=None, allow_lang_switch=True,
    )  # fmt: skip
    assert seen == {
        "video": 1080, "container": 1080, "audio": 1080, "langs": 1080, "resolver": 1080
    }  # fmt: skip


def test_no_mirror_suppresses_pathological_auto_switch(monkeypatch, capsys):
    """TONIGHT's scenario: pathological 4K Dolby pick would auto-mirror (ADR 0015);
    --no-mirror (tri-state False) must suppress the auto-switch per invocation —
    no config editing — and let the remux proceed."""
    stream = {
        "name": "[RD+] T\n2160p",
        "title": "Movie 4K REMUX\n💾 55 GB",
        "url": "http://u/m.mkv",
    }
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=0))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror", _boom("--no-mirror must suppress the mirror")
    )
    monkeypatch.setattr(
        cast_flow.remux, "remux_for_cast",
        lambda url, cfg, *, audio_index, size_gb=0.0: "/tmp/out.mp4",
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.subs, "align_local", lambda cfg, pick, m, wd, o: pick)
    monkeypatch.setattr(
        cast_flow.remux, "cast_file",
        lambda cfg, title, path, **k: seen.update(path=path) or _ok(subs=True),
    )  # fmt: skip
    out = _run(_opts(mirror=False), stream)  # tri-state False = --no-mirror
    assert seen["path"] == "/tmp/out.mp4" and out.action == "cast" and out.reencoded


def test_no_mirror_with_undecodable_video_raises(monkeypatch):
    """--no-mirror + video the DMR can't render: the explicit user intent wins — fail
    explicitly instead of silently mirroring (manual always wins)."""
    stream: Stream = _STREAM.copy()
    _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(
        cast_flow.cast_vet, "vet_cast_video",
        lambda cfg, results, chosen, exact_resolution=0, **_kw: (chosen, "mpeg4"),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    with pytest.raises(cast_flow.CastVideoUnsupported):
        _run(_opts(mirror=False), stream)


def test_absent_safety_subs_reported_when_actually_delivered(monkeypatch, capsys):
    """Twin of `test_absent_safety_subs_once_with_notice`: when the fetch DOES deliver a
    track, the confirmation line appears — proving the message follows the SubsPick and is
    not merely suppressed everywhere."""
    stream: Stream = _STREAM.copy()
    seen = _wire(monkeypatch, _plan("absent", stream, real_lang="eng"))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("no remux for an absent language"))
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(cast_url=a[2]) or _ok(),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.subs, "auto_subs",
        lambda cfg, typ, vid, wd, opts, safety_sub_lang=None, **kw: (
            seen["subs"].append(safety_sub_lang) or subs.SubsPick(paths=("/tmp/s.srt",), match="lang")
        ),
    )  # fmt: skip
    _run(_opts(), stream)
    err = capsys.readouterr().err
    assert "non disponibile" in err
    assert f"sottotitoli {CFG.primary} attivati" in err


def test_run_cast_threads_expected_runtime_to_all_vets(monkeypatch):
    """ADR 0028: every cast reselect must know the expected runtime, or a placeholder can
    win the swap even though `prepare_stream` just rejected one."""
    stream: Stream = _STREAM.copy()
    seen = {}
    monkeypatch.setattr(
        cast_flow.cast_vet, "vet_cast_video",
        lambda cfg, results, chosen, exact_resolution=0, expected_s=0.0, **_kw: (
            seen.update(video=expected_s) or (chosen, "")
        ),
    )  # fmt: skip
    monkeypatch.setattr(
        cast_flow.cast_vet, "vet_cast_container",
        lambda cfg, results, chosen, target, exact_resolution=0, expected_s=0.0, **_kw: (
            seen.update(container=expected_s) or (chosen, False)
        ),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.cast_vet, "cast_container", lambda cfg, s: "mp4")
    monkeypatch.setattr(
        cast_flow.cast_vet, "vet_cast_audio",
        lambda cfg, results, chosen, lang, exact_resolution=0, expected_s=0.0, **_kw: (
            seen.update(audio=expected_s) or _plan("direct", stream)
        ),
    )  # fmt: skip
    monkeypatch.setattr(cast_flow.subs, "auto_subs", lambda *a, **k: subs.SubsPick())
    monkeypatch.setattr(cast_flow.engine, "detach_spawned", lambda: None)
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok())
    cast_flow.run_cast(
        CFG, [stream], stream,
        device="192.168.1.5", title="T", typ="movie", video_id="tt1",
        work_dir="/tmp", opts=_opts(quality=1080), start=None,
        expected_runtime_s=3300.0,
    )  # fmt: skip
    assert seen == {"video": 3300.0, "container": 3300.0, "audio": 3300.0}


# --- advance parity across every delivery backend (ADR 0029) -----------------


def _wire_backend(monkeypatch, backend, stream, *, pos, dur):
    """Wire run_cast so `backend` ("direct" | "remux" | "mirror") is the one that delivers,
    reporting (pos, dur). Returns the spy dict from `_wire`."""
    if backend == "direct":
        seen = _wire(monkeypatch, _plan("direct", stream))
        monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: _ok(pos, dur))
    elif backend == "remux":
        seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
        monkeypatch.setattr(cast_flow.remux, "remux_for_cast", lambda url, cfg, **k: "/tmp/out.mp4")
        monkeypatch.setattr(cast_flow.remux, "cast_file", lambda *a, **k: _ok(pos, dur))
        monkeypatch.setattr(cast_flow.caster, "cast", _boom("remux must deliver"))
    else:
        seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
        monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
        monkeypatch.setattr(cast_flow.mirror, "cast_via_mirror", lambda *a, **k: _ok(pos, dur))
        monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("mirror must preempt"))
        monkeypatch.setattr(cast_flow.caster, "cast", _boom("mirror must deliver"))
    return seen


@pytest.mark.parametrize("backend", ["direct", "remux", "mirror"])
def test_advance_parity_across_backends(monkeypatch, backend):
    """The bug this pins (field-found 2026-08-04): the Tier-2 remux and the mirror returned
    `advance=False` hardcoded, so a binge on the TV died after one episode whenever the audio
    needed a remux. `advance` is now derived in cast_flow from what the backend observed, so
    every delivery path answers identically."""
    stream: Stream = _STREAM.copy()
    opts = _opts(mirror=True) if backend == "mirror" else _opts()
    _wire_backend(monkeypatch, backend, stream, pos=98.0, dur=100.0)
    out = _run(opts, stream, next_label="S01E02")
    assert out.advance is True  # ended past CAST_DONE with an episode queued


@pytest.mark.parametrize("backend", ["direct", "remux", "mirror"])
def test_no_advance_without_a_next_episode(monkeypatch, backend):
    stream: Stream = _STREAM.copy()
    opts = _opts(mirror=True) if backend == "mirror" else _opts()
    _wire_backend(monkeypatch, backend, stream, pos=98.0, dur=100.0)
    assert _run(opts, stream).advance is False  # a film that ended is not a binge


@pytest.mark.parametrize("backend", ["direct", "remux", "mirror"])
def test_no_advance_on_early_stop(monkeypatch, backend):
    stream: Stream = _STREAM.copy()
    opts = _opts(mirror=True) if backend == "mirror" else _opts()
    _wire_backend(monkeypatch, backend, stream, pos=50.0, dur=100.0)
    assert _run(opts, stream, next_label="S01E02").advance is False


@pytest.mark.parametrize("backend", ["direct", "remux", "mirror"])
def test_no_advance_when_position_was_never_observed(monkeypatch, backend):
    """Fire-and-return: nobody polled, so (0, 0) comes back. An unobserved position must
    never read as a finished episode."""
    stream: Stream = _STREAM.copy()
    opts = _opts(mirror=True) if backend == "mirror" else _opts()
    _wire_backend(monkeypatch, backend, stream, pos=0.0, dur=0.0)
    assert _run(opts, stream, next_label="S01E02").advance is False


def test_run_cast_raises_when_settled_stream_unresolved(monkeypatch):
    """The KeyError: 'url' crash (2026-08-08). If the audio reselect lands on a candidate
    that never resolved, every delivery branch would dereference a missing key. run_cast
    fails once, honestly, at the settle point — before any backend or side effect."""
    stream: Stream = _STREAM.copy()
    dead = {"infoHash": "deadbeef", "title": "Film.2006.iTA-GRP"}  # no url
    _wire(monkeypatch, _plan("direct", dead))
    monkeypatch.setattr(cast_flow.caster, "cast", _boom("must not cast an unresolved stream"))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("must not remux without a url"))
    monkeypatch.setattr(cast_flow.mirror, "cast_via_mirror", _boom("must not mirror without a url"))
    # Unlike CastVideoUnsupported this cannot be side-effect free: the stream only settles
    # after the audio reselect, which is downstream of the session clear (cast_flow.py:165).
    # No backend runs, which is the guarantee that matters.
    with pytest.raises(cast_flow.CastStreamUnresolved):
        _run(_opts(), stream)
