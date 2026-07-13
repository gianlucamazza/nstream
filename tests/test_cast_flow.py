"""Unit tests for the shared cast decision tree (`cast_flow.run_cast`).

These exercise the helper directly (the wrapper wiring — `cli._play_on_cast` and the
headless `--json --cast` branch — stays covered in test_cli.py). Patch seams are the
module attributes cast_flow re-imports: `cast_flow.stream_select` / `remux` / `mirror` /
`caster` / `subs` / `engine`."""

from __future__ import annotations

import pytest

from nstream import cast_flow
from nstream.config import Config, PlayOpts

CFG = Config(torrentio_base="tb", subtitle_langs=["ita", "eng"])

_STREAM = {
    "name": "[RD+] Torrentio\n1080p",
    "title": "Dune.2024.1080p.WEB-DL.HEVC.ITA-GRP\n👤 9 💾 8 GB",
    "url": "http://u/dune.mkv",
}


def _opts(**kw):
    base = dict(auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False)
    base.update(kw)
    return PlayOpts(**base)


def _plan(mode, stream, audio_index=0, real_lang="ita", verified=True):
    return cast_flow.stream_select.CastAudioPlan(
        mode, stream, audio_index, real_lang, verified=verified
    )


def _boom(msg):
    def fail(*a, **k):
        raise AssertionError(msg)

    return fail


def _wire(monkeypatch, plan, *, langs=("ita",)):
    """Hermetic run_cast: stub vet_cast_audio (the real one ffprobes the url), the in-cast
    switch helpers, auto_subs and detach_spawned. Returns the spy dict."""
    seen = {"subs": [], "detached": 0}
    monkeypatch.setattr(cast_flow.stream_select, "vet_cast_audio", lambda *a, **k: plan)
    monkeypatch.setattr(cast_flow.stream_select, "cast_languages", lambda cfg, results: langs)
    monkeypatch.setattr(
        cast_flow.stream_select, "cast_resolver", lambda cfg, results: lambda lang: "http://u2"
    )
    monkeypatch.setattr(
        cast_flow.subs, "auto_subs",
        lambda cfg, typ, vid, wd, opts, safety_sub_lang=None: (
            seen["subs"].append(safety_sub_lang) or ()
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
    stream = dict(_STREAM)
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
            or (0.0, 0.0, False, False)
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
    stream = dict(_STREAM)
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", lambda *a, **k: None)  # ffmpeg failed
    monkeypatch.setattr(
        cast_flow.remux, "cast_file", _boom("cast_file must not run without a remux")
    )
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(cast_url=a[2]) or (0.0, 0.0, False, False),
    )  # fmt: skip
    out = _run(_opts(), stream)
    assert "remux non riuscito" in capsys.readouterr().err
    assert seen["cast_url"] == stream["url"]
    assert out.notice and "remux non riuscito" in out.notice
    assert out.reencoded is False and out.action == "cast"


# --- absent dub (normalizations 1 + 2) ---------------------------------------


def test_absent_safety_subs_once_with_notice(monkeypatch, capsys):
    """No dub carries the primary language → auto_subs runs exactly ONCE with the safety
    language, stderr explains (on every path), and the pick is cast directly anyway."""
    stream = dict(_STREAM)
    seen = _wire(monkeypatch, _plan("absent", stream, real_lang="eng"))
    monkeypatch.setattr(cast_flow.remux, "remux_for_cast", _boom("no remux for an absent language"))
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: seen.update(cast_url=a[2]) or (0.0, 0.0, False, False),
    )  # fmt: skip
    out = _run(_opts(), stream)
    err = capsys.readouterr().err
    assert seen["subs"] == [CFG.primary]  # ONE call, safety lang set (normalization 2)
    assert "non disponibile" in err and f"sottotitoli {CFG.primary}" in err
    assert seen["cast_url"] == stream["url"]
    assert out.safety_sub_lang == CFG.primary
    assert out.audio_lang == "eng" and out.audio_verified is True


def test_safety_sub_lang_passthrough(monkeypatch):
    """A caller-provided safety language (from prepare_stream's vet) reaches auto_subs and
    the outcome untouched when the cast plan isn't `absent`."""
    stream = dict(_STREAM)
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: (0.0, 0.0, False, False))
    out = _run(_opts(), stream, safety_sub_lang="ita")
    assert seen["subs"] == ["ita"]
    assert out.safety_sub_lang == "ita"


# --- mirror gate --------------------------------------------------------------


def test_mirror_gates_on_remux_audio(monkeypatch):
    """--mirror + undecodable audio (plan remux) → cast_via_mirror with follow threaded;
    remux and the direct cast must not run."""
    stream = dict(_STREAM)
    seen = _wire(monkeypatch, _plan("remux", stream, audio_index=1))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror",
        lambda cfg, title, url, **k: (
            seen.update(
                url=url, device=k.get("device"), start=k.get("start"), follow=k.get("follow")
            )
            or (0.0, 0.0, False)
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


def test_mirror_downgraded_when_decodable(monkeypatch, capsys):
    """--mirror with DMR-decodable audio (plan direct) → transparent direct cast + the
    single shared notice (normalization 3)."""
    stream = dict(_STREAM)
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.mirror, "available", lambda: True)
    monkeypatch.setattr(
        cast_flow.mirror, "cast_via_mirror", _boom("mirror must not run for decodable audio")
    )
    monkeypatch.setattr(
        cast_flow.caster, "cast", lambda *a, **k: seen.update(cast=True) or (0.0, 0.0, False, False)
    )
    out = _run(_opts(mirror=True), stream)
    assert cast_flow.MIRROR_NOT_NEEDED in capsys.readouterr().err
    assert seen.get("cast") is True and out.action == "cast"


# --- in-cast audio switch wiring ----------------------------------------------


def test_lang_switch_wired_only_when_allowed(monkeypatch):
    """allow_lang_switch=True with several dubs → langs + resolver reach the cast; with a
    single language both stay empty; headless (False) never pays the extra rank pass."""
    stream = dict(_STREAM)
    seen = _wire(monkeypatch, _plan("direct", stream), langs=("ita", "eng"))
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: (
            seen.update(langs=k.get("langs"), resolver=k.get("resolve_lang")) or (0.0, 0.0, False, False)
        ),
    )  # fmt: skip
    _run(_opts(), stream, allow_lang_switch=True)
    assert seen["langs"] == ("ita", "eng") and callable(seen["resolver"])
    monkeypatch.setattr(cast_flow.stream_select, "cast_languages", lambda cfg, results: ("ita",))
    _run(_opts(), stream, allow_lang_switch=True)
    assert seen["langs"] == () and seen["resolver"] is None
    monkeypatch.setattr(
        cast_flow.stream_select, "cast_languages", _boom("headless must skip the rank pass")
    )
    _run(_opts(), stream)  # allow_lang_switch defaults to False
    assert seen["langs"] == () and seen["resolver"] is None


def test_next_label_and_meta_threaded(monkeypatch):
    """next_label/meta (the interactive knobs) reach the direct cast untouched."""
    stream = dict(_STREAM)
    seen = _wire(monkeypatch, _plan("direct", stream))
    meta = cast_flow.caster.CastMeta(poster="http://img/p.jpg")
    monkeypatch.setattr(
        cast_flow.caster, "cast",
        lambda *a, **k: (
            seen.update(next_label=k.get("next_label"), meta=k.get("meta"))
            or (1.0, 2.0, True, False)
        ),
    )  # fmt: skip
    out = _run(_opts(), stream, next_label="S01E02", meta=meta)
    assert seen["next_label"] == "S01E02" and seen["meta"] is meta
    assert (out.pos, out.dur, out.advance) == (1.0, 2.0, True)


# --- fire-and-return handoff ---------------------------------------------------


@pytest.mark.parametrize("follow,expected", [(True, 0), (False, 1)])
def test_detach_spawned_gated_on_follow(monkeypatch, follow, expected):
    """The TorrServer handoff (engine.detach_spawned) runs inside the helper, only on
    fire-and-return (follow=False) — interactive follow keeps ownership."""
    stream = dict(_STREAM)
    seen = _wire(monkeypatch, _plan("direct", stream))
    monkeypatch.setattr(cast_flow.caster, "cast", lambda *a, **k: (0.0, 0.0, False, False))
    _run(_opts(), stream, follow=follow)
    assert seen["detached"] == expected
