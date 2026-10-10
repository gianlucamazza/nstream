"""Unit tests for Chromecast playback via catt: device resolution, the cast() poll
loop (resume/auto-advance), and the in-cast audio-language switch."""

from __future__ import annotations

import io
import json
import subprocess
import time
import types
from pathlib import Path

import pytest

from nstream import _catt_load, cast_delivery, caster
from nstream.config import Config

_DATA = Path(__file__).resolve().parent / "data"

CFG = Config(torrentio_base="tb")


# --- device resolution -----------------------------------------------------


@pytest.fixture(autouse=True)
def _catt_on_path(monkeypatch):
    """resolve_device guards on catt's presence; keep tests hermetic (the makepkg
    check() chroot, for one, has no catt installed)."""
    monkeypatch.setattr(caster.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")


@pytest.fixture(autouse=True)
def _no_catt_lib(monkeypatch):
    """Default: no catt.api in the test env. Tests that pin the library LOAD
    override `catt_can_lib_load` / `catt_lib_outcome` / `_catt_device_cls`."""
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: None)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: None)


@pytest.fixture(autouse=True)
def _catt_meta_ok(monkeypatch):
    """Hermetic: treat the test host as catt ≥0.13.2 so `-l` / `--stream-type` stay on."""
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: True)
    monkeypatch.setattr(caster, "catt_inprocess_supports_load_meta", lambda: True)


@pytest.fixture(autouse=True)
def _no_lan_proxy(monkeypatch):
    """Existing cast() tests hand catt a stub url (`http://u`). A real LAN wrap would
    probe it. Phase-1 tests live in test_lan_proxy; this keeps the catt argv
    assertions hermetic."""
    monkeypatch.setattr(caster, "lan_media", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_discovery_io(monkeypatch):
    """Keep tests hermetic: no disk cache, no background thread, no TCP probe.
    Individual tests override get_devices (via _scan) / load_cache / verify."""
    monkeypatch.setattr(caster.discovery, "load_cache", lambda: [])
    monkeypatch.setattr(caster.discovery, "start_background", lambda: None)
    monkeypatch.setattr(caster.discovery, "get_devices", lambda wait=0.0: ([], "fresh"))
    monkeypatch.setattr(caster.discovery, "verify", lambda ip, **k: False)


def _scan(devs):
    # The background scan finished with `devs`: the seam resolve_device consults.
    return lambda wait=0.0: (list(devs), "fresh")  # devs: [(name, ip)]


def test_resolve_device_missing_catt_says_so(monkeypatch):
    """A missing catt binary must surface as such — not masquerade as an empty network
    (run_cmd swallows the OSError, so the scan would just look instantly empty)."""
    monkeypatch.setattr(caster.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(
        caster.discovery, "get_devices", lambda *a, **k: pytest.fail("must not scan")
    )
    with pytest.raises(caster.CastUnavailable, match="catt non trovato"):
        caster.resolve_device(CFG)


def test_resolve_device_pref_present_returns_ip(monkeypatch):
    monkeypatch.setattr(
        caster.discovery,
        "get_devices",
        _scan([("Salotto", "192.168.1.5"), ("Camera", "192.168.1.6")]),
    )
    cfg = Config(torrentio_base="tb", cast_device="Salotto")
    assert caster.resolve_device(cfg) == "192.168.1.5"  # preferred name → its current IP


def test_resolve_device_pref_absent_rediscovers(monkeypatch):
    # Pinned name not on this LAN (network changed) → re-discover, don't return it stale.
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("Camera", "192.168.1.6")]))
    cfg = Config(torrentio_base="tb", cast_device="Salotto")
    assert caster.resolve_device(cfg) == "192.168.1.6"


def test_resolve_device_single_auto_ip(monkeypatch):
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("TV1", "10.0.0.9")]))
    assert caster.resolve_device(Config(torrentio_base="tb")) == "10.0.0.9"


# --- auto-cast confirmation (injected by the frontend, ADR 0037) -------------


def test_resolve_device_confirm_accepted(monkeypatch):
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("TV1", "10.0.0.9")]))
    ip = caster.resolve_device(Config(torrentio_base="tb"), confirm=lambda name, ip: True)
    assert ip == "10.0.0.9"


def test_resolve_device_confirm_declined_raises(monkeypatch):
    # A declined confirmation must fall back like an absent device (local playback).
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("TV1", "10.0.0.9")]))
    with pytest.raises(caster.CastUnavailable, match="rifiutato"):
        caster.resolve_device(Config(torrentio_base="tb"), confirm=lambda name, ip: False)


def test_resolve_device_confirm_preferred_device(monkeypatch):
    monkeypatch.setattr(
        caster.discovery,
        "get_devices",
        _scan([("Salotto", "192.168.1.5"), ("Camera", "192.168.1.6")]),
    )
    asked = []
    cfg = Config(torrentio_base="tb", cast_device="Salotto")
    ip = caster.resolve_device(cfg, confirm=lambda name, ip: not asked.append((name, ip)))
    assert ip == "192.168.1.5"
    assert asked == [("Salotto", "192.168.1.5")]


def test_resolve_device_headless_never_confirms(monkeypatch):
    # ADR 0037: a caller that injects no prompt can't be prompted.
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("TV1", "10.0.0.9")]))
    assert caster.resolve_device(Config(torrentio_base="tb"), headless=True) == "10.0.0.9"


def test_resolve_device_multiple_prompts_ip(monkeypatch):
    monkeypatch.setattr(
        caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")])
    )
    ip = caster.resolve_device(Config(torrentio_base="tb"), picker=lambda devices: "10.0.0.2")
    assert ip == "10.0.0.2"


def test_resolve_device_choose_forces_picker_by_name(monkeypatch):
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1")]))
    seen = {}

    def pick(devices):
        seen["items"] = devices
        return "10.0.0.1"

    assert caster.resolve_device(Config(torrentio_base="tb"), choose=True, picker=pick) == (
        "10.0.0.1"
    )
    assert seen["items"] == [("TV1", "10.0.0.1")]  # label=name, value=ip


def test_resolve_device_ambiguous_without_picker_raises(monkeypatch):
    monkeypatch.setattr(
        caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")])
    )
    with pytest.raises(caster.CastUnavailable, match="--device"):
        caster.resolve_device(Config(torrentio_base="tb"))


def test_resolve_device_none_raises(monkeypatch):
    # Empty scan → trust it (no fall-through to a stale cast-resolve default).
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([]))
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(Config(torrentio_base="tb"))


def test_resolve_device_cancel_raises(monkeypatch):
    monkeypatch.setattr(
        caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")])
    )
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(Config(torrentio_base="tb"), picker=lambda devices: None)


# --- discovery cache fast paths / non-blocking guarantees -------------------


def _must_not_wait(*a, **k):
    pytest.fail("must not wait on the background scan")


def test_resolve_device_cached_target_instant(monkeypatch):
    # A cache-verified preferred device resolves instantly, no scan wait at all.
    monkeypatch.setattr(caster.discovery, "load_cache", lambda: [("Salotto", "192.168.1.5")])
    monkeypatch.setattr(caster.discovery, "verify", lambda ip, **k: True)
    monkeypatch.setattr(caster.discovery, "get_devices", _must_not_wait)
    cfg = Config(torrentio_base="tb", cast_device="Salotto")
    assert caster.resolve_device(cfg) == "192.168.1.5"


def test_resolve_device_cached_single_instant(monkeypatch):
    # The common single-TV home: the lone cached device, verified, is used right away.
    monkeypatch.setattr(caster.discovery, "load_cache", lambda: [("TV1", "10.0.0.9")])
    monkeypatch.setattr(caster.discovery, "verify", lambda ip, **k: True)
    monkeypatch.setattr(caster.discovery, "get_devices", _must_not_wait)
    assert caster.resolve_device(Config(torrentio_base="tb")) == "10.0.0.9"


def test_resolve_device_cached_target_dead_falls_to_scan(monkeypatch):
    # Cached IP no longer answers (DHCP moved it) → trust the fresh scan instead.
    monkeypatch.setattr(caster.discovery, "load_cache", lambda: [("Salotto", "192.168.1.5")])
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("Salotto", "192.168.1.99")]))
    cfg = Config(torrentio_base="tb", cast_device="Salotto")
    assert caster.resolve_device(cfg) == "192.168.1.99"


def test_resolve_device_empty_scan_rescued_by_cache(monkeypatch):
    # TV alive (TCP 8009 answers) but the fresh mDNS scan came back empty → the cache
    # rescues the cast instead of failing it.
    monkeypatch.setattr(
        caster.discovery, "load_cache", lambda: [("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")]
    )
    monkeypatch.setattr(caster.discovery, "verify", lambda ip, **k: ip == "10.0.0.2")
    assert caster.resolve_device(Config(torrentio_base="tb")) == "10.0.0.2"


def test_resolve_device_empty_scan_dead_cache_raises(monkeypatch):
    # Nothing scanned and nothing cached answers → fail fast (caller plays locally).
    monkeypatch.setattr(
        caster.discovery, "load_cache", lambda: [("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")]
    )
    with pytest.raises(caster.CastUnavailable, match="nessun Chromecast"):
        caster.resolve_device(Config(torrentio_base="tb"))


def test_resolve_device_ctrl_c_skips_to_local(monkeypatch):
    # Ctrl-C while waiting on a pending scan = "skip the cast", not a process abort.
    calls = []

    def fake_get(wait=0.0):
        calls.append(wait)
        if len(calls) == 1:
            return ([], "pending")
        raise KeyboardInterrupt

    monkeypatch.setattr(caster.discovery, "get_devices", fake_get)
    with pytest.raises(caster.CastUnavailable, match="annullata"):
        caster.resolve_device(Config(torrentio_base="tb"))
    assert calls == [0.0, caster._WAIT_RESOLVE]  # short interactive budget, not a full scan


def test_resolve_device_pending_timeout_raises_fast(monkeypatch):
    # Scan still pending after the short budget and no cache → fail fast, no 40s freeze.
    monkeypatch.setattr(caster.discovery, "get_devices", lambda wait=0.0: ([], "pending"))
    with pytest.raises(caster.CastUnavailable, match="nessun Chromecast"):
        caster.resolve_device(Config(torrentio_base="tb"))


def test_resolve_device_headless_blocks_until_done(monkeypatch):
    # Headless callers wait for the full scan (deterministic for scripts): wait=None.
    waits = []

    def fake_get(wait=0.0):
        waits.append(wait)
        if len(waits) == 1:
            return ([], "pending")
        return ([("TV1", "10.0.0.1")], "fresh")

    monkeypatch.setattr(caster.discovery, "get_devices", fake_get)
    assert caster.resolve_device(CFG, headless=True) == "10.0.0.1"
    assert waits == [0.0, None]


# --- cast() poll loop ------------------------------------------------------


def _cast_run(monkeypatch, *, launch_rc=0, info_seq=()):
    """Stub subprocess.run for cast(): the first call is `catt cast` (returns
    launch_rc), subsequent `catt ... info -j` calls yield info_seq JSON in order.
    Records every argv. _poll_wait is neutralised."""
    import json as _json

    calls = []
    seq = list(info_seq)

    class _P:
        def __init__(self, rc, out=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    def fake(cmd, **k):
        calls.append(cmd)
        if "cast" in cmd:
            return _P(launch_rc)
        if "info" in cmd:
            if seq:
                item = seq.pop(0)
                return _P(0, _json.dumps(item)) if item is not None else _P(1)
            return _P(1)  # device idle/unreachable
        return _P(0)

    monkeypatch.setattr(caster.subprocess, "run", fake)
    monkeypatch.setattr(caster, "_poll_wait", lambda *_: None)
    return calls


def test_cast_builds_command_with_seek_and_sub(monkeypatch):
    calls = _cast_run(
        monkeypatch,
        info_seq=[
            {"player_state": "PLAYING", "current_time": 1.0, "duration": 100.0},
            {"player_state": "IDLE", "duration": 100.0},  # ended → exits cleanly
        ],
    )
    caster.cast(
        CFG, "Dune", "http://u",
        device="TV", start=125.0, sub_paths=("/tmp/x.srt",),
    )  # fmt: skip
    launch = calls[0]
    assert launch[:2] == ["catt", "-d"] and launch[2] == "TV"
    assert "cast" in launch and "http://u" in launch
    assert "-t" in launch and "125" in launch
    assert "-s" in launch and "/tmp/x.srt" in launch
    assert launch[launch.index("-l") + 1] == "Dune"
    assert launch[launch.index("--stream-type") + 1] == "BUFFERED"


def test_cast_tracks_position_to_the_end(monkeypatch):
    """caster reports what it observed; whether that counts as a finished episode is
    `cast_flow`'s call (ADR 0029) — see the parity pin in tests/test_cast_flow.py."""
    _cast_run(
        monkeypatch,
        info_seq=[
            {"player_state": "PLAYING", "current_time": 10.0, "duration": 100.0},
            {"player_state": "PLAYING", "current_time": 99.0, "duration": 100.0},
            {"player_state": "IDLE", "duration": 100.0},
        ],
    )
    r = caster.cast(CFG, "Show E1", "http://u", device="TV")
    assert (r.pos, r.dur) == (99.0, 100.0)
    assert r.started is True and r.error is None


def test_cast_reports_early_stop_position(monkeypatch):
    _cast_run(
        monkeypatch,
        info_seq=[
            {"player_state": "PLAYING", "current_time": 20.0, "duration": 100.0},
            {"player_state": "IDLE", "duration": 100.0},  # stopped at 20% → not finished
        ],
    )
    r = caster.cast(CFG, "Show E1", "http://u", device="TV")
    assert (r.pos, r.dur) == (20.0, 100.0)
    assert r.started is True


def test_cast_launch_failure_reports_not_started(monkeypatch):
    """`catt cast` exiting non-zero used to return the same (0.0, 0.0) a legitimate
    fire-and-return returns, so headless reported the dead cast as `ok: true` (ADR 0031)."""
    _cast_run(monkeypatch, launch_rc=1)
    r = caster.cast(CFG, "M", "http://u", device="TV")
    assert (r.pos, r.dur) == (0.0, 0.0)
    assert r.started is False and r.error == "cast_failed"


def test_cast_gives_up_if_never_starts(monkeypatch):
    # Receiver stays idle/unreachable forever → bail after _CAST_GIVEUP polls,
    # never loops indefinitely.
    calls = _cast_run(monkeypatch, info_seq=[])  # every info poll fails
    r = caster.cast(CFG, "M", "http://u", device="TV")
    assert r.started is False and r.error == "cast_never_started"
    info_polls = sum(1 for c in calls if "info" in c)
    assert info_polls == caster._CAST_GIVEUP


def test_cast_launch_timeout_degrades(monkeypatch):
    """A catt hung on a half-dead device must not block forever: the launch carries an
    explicit timeout and TimeoutExpired degrades to a clean failure (no exception)."""

    def hang(cmd, **k):
        assert k.get("timeout")  # the synchronous launch must have a deadline
        raise subprocess.TimeoutExpired(cmd, k["timeout"])

    monkeypatch.setattr(caster.subprocess, "run", hang)
    events = []
    result = caster.cast(CFG, "M", "http://u", device="TV", on_event=events.append)
    assert (result.pos, result.dur) == (0.0, 0.0)
    assert result.started is False and result.error == "cast_timeout"
    assert [e["kind"] for e in events] == ["failed"]


def test_cast_poll_timeout_counts_as_unreachable(monkeypatch):
    """An info poll that hangs (TimeoutExpired) counts as an unreachable device: the
    loop gives up after _CAST_GIVEUP polls instead of raising or spinning forever."""
    polls = []

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake(cmd, **k):
        if "info" in cmd:
            polls.append(cmd)
            raise subprocess.TimeoutExpired(cmd, k.get("timeout", 10))
        return _P()  # the launch succeeds

    monkeypatch.setattr(caster.subprocess, "run", fake)
    monkeypatch.setattr(caster, "_poll_wait", lambda *_: None)
    r = caster.cast(CFG, "M", "http://u", device="TV")
    assert r.started is False and r.error == "cast_never_started"
    assert len(polls) == caster._CAST_GIVEUP


def test_stop_and_volume_timeout_degrade(monkeypatch):
    def hang(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, k.get("timeout", 10))

    monkeypatch.setattr(caster.subprocess, "run", hang)
    assert caster.stop("1.2.3.4") is False
    assert caster.set_volume("1.2.3.4", 50) is False
    assert caster.status("1.2.3.4")["player_state"] == "IDLE"  # receiver_info degrades too


def test_cast_prints_preparing_before_launch(monkeypatch, capsys):
    _cast_run(monkeypatch, info_seq=[{"player_state": "IDLE"}])
    caster.cast(CFG, "Dune", "http://u", device="TV")
    err = capsys.readouterr().err
    assert "consegno" in err and "TV" in err
    assert "in onda" in err


def test_cast_warns_on_zero_volume(monkeypatch, capsys):
    _cast_run(
        monkeypatch,
        info_seq=[
            {"player_state": "PLAYING", "current_time": 5.0, "duration": 100.0, "volume_level": 0},
            {"player_state": "IDLE", "duration": 100.0},
        ],
    )
    caster.cast(CFG, "Dune", "http://u", device="TV")
    assert "volume del Chromecast a 0" in capsys.readouterr().err


def test_cast_no_volume_warning_when_audible(monkeypatch, capsys):
    _cast_run(
        monkeypatch,
        info_seq=[
            {
                "player_state": "PLAYING",
                "current_time": 5.0,
                "duration": 100.0,
                "volume_level": 0.4,
            },
            {"player_state": "IDLE", "duration": 100.0},
        ],
    )
    caster.cast(CFG, "Dune", "http://u", device="TV")
    assert "volume del Chromecast a 0" not in capsys.readouterr().err


# --- single-keypress poll + in-cast audio switch ---------------------------


def test_poll_wait_non_tty_sleeps(monkeypatch):
    monkeypatch.setattr(caster.sys.stdin, "isatty", lambda: False)
    slept = []
    monkeypatch.setattr(caster.time, "sleep", lambda t: slept.append(t))
    assert caster._poll_wait(15.0) is None
    assert slept == [15.0]


def test_cast_progress_from_remaining(monkeypatch):
    # When only `remaining` is reported, position derives from duration - remaining.
    pos, dur, state = caster._cast_progress(
        {"player_state": "PLAYING", "duration": 100.0, "remaining": 30.0}
    )
    assert (pos, dur, state) == (70.0, 100.0, "PLAYING")


def test_cast_hotkey_switches_audio(monkeypatch):
    calls = _cast_run(
        monkeypatch,
        info_seq=[
            {"player_state": "PLAYING", "current_time": 30.0, "duration": 100.0},
            {"player_state": "PLAYING", "current_time": 35.0, "duration": 100.0},
            {"player_state": "IDLE", "duration": 100.0},
        ],
    )
    keys = iter([None, "a", None, None, None])
    monkeypatch.setattr(caster, "_poll_wait", lambda _t: next(keys, None))
    caster.cast(
        CFG, "Film", "http://ita",
        device="TV", langs=("ita", "eng"), resolve_lang=lambda lang: "http://eng",
        choose_lang=lambda codes: "eng",
    )  # fmt: skip
    recasts = [c for c in calls if "cast" in c and "http://eng" in c]
    assert recasts and "-t" in recasts[0]  # re-cast the eng url with a seek


def test_cast_hotkey_lib_keeps_title(monkeypatch):
    """In-cast 'a' reuses the library LOAD so title/thumb are not dropped."""
    seen: dict = {}
    monkeypatch.setattr(caster, "catt_can_lib_load", lambda: True)

    def fake_play(device, url, **kw):
        seen.update(device=device, url=url, **kw)
        return caster.CATT_LIB_OK

    monkeypatch.setattr(caster, "catt_lib_outcome", fake_play)
    calls = _cast_run(
        monkeypatch,
        info_seq=[
            {"player_state": "PLAYING", "current_time": 30.0, "duration": 100.0},
            {"player_state": "PLAYING", "current_time": 35.0, "duration": 100.0},
            {"player_state": "IDLE", "duration": 100.0},
        ],
    )
    keys = iter(["a", None, None, None, None])
    monkeypatch.setattr(caster, "_poll_wait", lambda _t: next(keys, None))
    poster = "https://images.metahub.space/poster/medium/tt1/img"
    caster.cast(
        CFG, "Film", "http://ita",
        device="TV", langs=("ita", "eng"), resolve_lang=lambda lang: "http://eng",
        choose_lang=lambda codes: "eng",
        meta=caster.CastMeta(poster=poster),
    )  # fmt: skip
    assert seen["url"] == "http://eng" and seen["title"] == "Film"
    assert seen["meta"].poster == poster
    assert not [c for c in calls if "cast" in c and "http://eng" in c]


def test_cast_hotkey_esc_keeps_current(monkeypatch):
    calls = _cast_run(monkeypatch, info_seq=[{"player_state": "IDLE"}])
    keys = iter(["a", None, None, None, None])
    monkeypatch.setattr(caster, "_poll_wait", lambda _t: next(keys, None))
    resolved = []
    caster.cast(
        CFG, "Film", "http://ita",
        device="TV", langs=("ita", "eng"), resolve_lang=lambda lang: resolved.append(lang),
        choose_lang=lambda codes: None,  # ESC
    )  # fmt: skip
    assert resolved == []  # ESC → resolver never called, no re-cast
    assert not [c for c in calls if "cast" in c and "-t" in c]


# --- headless device resolution (no fzf) -----------------------------------


def test_resolve_device_headless_ambiguous_raises(monkeypatch):
    # ≥2 devices, no preference: headless must NOT open fzf — it raises so the caller
    # can surface a clean error and re-run with --device.
    monkeypatch.setattr(
        caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")])
    )

    def no_picker(devices):
        raise AssertionError("headless must not open a picker")

    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(CFG, headless=True, picker=no_picker)


def test_resolve_device_headless_prefers_named(monkeypatch):
    monkeypatch.setattr(
        caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1"), ("Salotto", "10.0.0.2")])
    )
    assert caster.resolve_device(CFG, headless=True, prefer="Salotto") == "10.0.0.2"


def test_resolve_device_prefer_absent_raises(monkeypatch):
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1")]))
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(CFG, prefer="Salotto")


# --- lifecycle actions: stop / status / set_volume -------------------------


def test_stop_runs_catt_stop(monkeypatch):
    seen = {}

    class _R:
        returncode = 0

    def run(cmd, **k):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(caster.subprocess, "run", run)
    assert caster.stop("1.2.3.4") is True
    assert seen["cmd"] == ["catt", "-d", "1.2.3.4", "stop"]


def test_set_volume_clamps_and_calls(monkeypatch):
    seen = {}

    class _R:
        returncode = 0

    monkeypatch.setattr(caster.subprocess, "run", lambda cmd, **k: seen.update(cmd=cmd) or _R())
    assert caster.set_volume("1.2.3.4", 150) is True  # clamped to 100
    assert seen["cmd"] == ["catt", "-d", "1.2.3.4", "volume", "100"]


def test_volume_percent_level_roundtrip_and_phase0_quantization():
    """CLI 0–100% ↔ Cast 0–1. Phase 0 readback ≈0.133 displays as 13, not 14."""
    assert caster.clamp_volume_percent(150) == 100
    assert caster.clamp_volume_percent(-3) == 0
    assert caster.clamp_volume_percent(14) == 14
    assert caster.volume_percent_to_level(0) == 0.0
    assert caster.volume_percent_to_level(14) == 0.14
    assert caster.volume_percent_to_level(25) == 0.25
    assert caster.volume_percent_to_level(50) == 0.5
    assert caster.volume_percent_to_level(100) == 1.0
    assert caster.volume_level_to_percent(0.0) == 0
    assert caster.volume_level_to_percent(0.14) == 14  # do not truncate 13.999…
    assert caster.volume_level_to_percent(0.25) == 25
    assert caster.volume_level_to_percent(0.5) == 50
    assert caster.volume_level_to_percent(1.0) == 100
    assert caster.volume_level_to_percent(0.13333334028720856) == 13
    assert caster.volume_level_to_percent(None) is None
    assert caster.volume_level_to_percent(35) == 35  # already-percent defensive
    bits = caster.format_volume_bits(0.13333334028720856, control_type="master")
    assert bits == "vol 13% · master · step=null · ≠ OSD"
    assert caster.format_volume_bits(0.5, control_type="master", step_interval=0.05) == (
        "vol 50% · master · step=0.05 · ≠ OSD"
    )
    assert caster.format_volume_bits(0.4, osd_mismatch=False) == "vol 40%"


def test_status_normalizes(monkeypatch):
    info = {
        "player_state": "PLAYING",
        "current_time": 12.0,
        "duration": 100.0,
        "media_metadata": {"title": "The Matrix"},
        "volume_level": 0.4,
        "volume_muted": False,
        "volume_control_type": "master",
        "volume_step_interval": 0.05,
        "app_id": "CC1AD845",
        "content_type": "video/mp4",
        "stream_type": "BUFFERED",
        "content_id": "https://debrid.example/token",  # must never leak into status
    }

    class _R:
        returncode = 0
        stdout = __import__("json").dumps(info)

    monkeypatch.setattr(caster.subprocess, "run", lambda cmd, **k: _R())
    st = caster.status("1.2.3.4")
    assert st["player_state"] == "PLAYING" and st["title"] == "The Matrix"
    assert st["volume"] == 0.4 and st["volume_percent"] == 40 and st["muted"] is False
    assert st["volume_control_type"] == "master"
    assert st["volume_step_interval"] == 0.05
    assert st["app_id"] == "CC1AD845"
    assert st["content_type"] == "video/mp4" and st["stream_type"] == "BUFFERED"
    assert "content_id" not in st


def test_status_idle_on_failure(monkeypatch):
    def boom(cmd, **k):
        raise OSError("no catt")

    monkeypatch.setattr(caster.subprocess, "run", boom)
    st = caster.status(None)
    assert st["player_state"] == "IDLE"


# --- receiver track/error observability in --status (ADR 0016) --------------


def test_status_receiver_track_fields_default(monkeypatch):
    """With the bridge unavailable (conftest default) --status still carries the fields, empty:
    active_tracks=[] and receiver_error=None (no downgrade, predictable shape)."""

    def boom(cmd, **k):
        raise OSError("no catt")

    monkeypatch.setattr(caster.subprocess, "run", boom)
    st = caster.status(None)
    assert st["active_tracks"] == [] and st["receiver_error"] is None


def test_bridge_track_info_reads_session(monkeypatch):
    """When the bridge is up, _bridge_track_info extracts the receiver's confirmed
    activeTrackIds + error from its session snapshot (ADR 0016)."""
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(
        caster.bridge, "peek_status",
        lambda device: {"session": "media", "media": {"activeTrackIds": [1], "error": "ERROR"}},
    )  # fmt: skip
    tracks, err = caster._bridge_track_info("1.2.3.4")
    assert tracks == [1] and err == "ERROR"


def test_bridge_track_info_empty_when_unavailable(monkeypatch):
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(
        caster.bridge, "peek_status", lambda device: pytest.fail("must not query a dead bridge")
    )
    assert caster._bridge_track_info("1.2.3.4") == ([], None)


# --- cast() dispatch: castbridge vs catt -----------------------------------


def test_cast_prefers_bridge_with_metadata(monkeypatch):
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: True)

    def fake_load(ip, url, *, follow=True, **meta):
        assert meta["poster"] == "p.jpg" and meta["title"] == "Dune"
        yield {"kind": "started", "title": "Dune"}
        yield {"kind": "playing", "position": 10.0, "duration": 100.0}
        yield {"kind": "ended", "position": 98.0, "duration": 100.0}

    monkeypatch.setattr(caster.bridge, "cast_load", fake_load)
    seen = []
    r = caster.cast(
        CFG,
        "Dune",
        "http://x",
        device="1.2.3.4",
        meta=caster.CastMeta(poster="p.jpg"),
        on_event=seen.append,
    )
    assert (r.pos, r.dur) == (98.0, 100.0)
    assert r.started is True and r.error is None
    assert [e["kind"] for e in seen] == ["started", "playing", "ended"]


def test_cast_forwards_custom_receiver_app_id(monkeypatch):
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: True)
    seen: dict = {}

    def fake_load(ip, url, *, follow=True, **meta):
        seen.update(meta)
        yield {"kind": "started", "title": "Dune"}
        yield {"kind": "ended", "position": 1.0, "duration": 1.0}

    monkeypatch.setattr(caster.bridge, "cast_load", fake_load)
    cfg = Config(torrentio_base="tb", cast_receiver_app_id="CA5T0001")
    with caster.notices.capture() as bag:
        caster.cast(cfg, "Dune", "http://x", device="1.2.3.4")
    assert seen.get("app_id") == "CA5T0001"
    assert all(n.code != "receiver_app_ignored" for n in bag)


def test_catt_path_warns_when_custom_receiver_ignored(monkeypatch):
    """catt cannot launch cast_receiver_app_id (ADR 0045): say so, stay on CC1AD845."""
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(
        caster,
        "_cast_via_catt",
        lambda *a, **k: cast_delivery.CastResult(0.0, 0.0, started=True),
    )
    cfg = Config(torrentio_base="tb", cast_receiver_app_id="07841171")
    with caster.notices.capture() as bag:
        caster.cast(cfg, "Dune", "http://x", device="1.2.3.4")
    assert any(n.code == "receiver_app_ignored" for n in bag)


def test_catt_path_silent_when_no_custom_receiver(monkeypatch):
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(
        caster,
        "_cast_via_catt",
        lambda *a, **k: cast_delivery.CastResult(0.0, 0.0, started=True),
    )
    with caster.notices.capture() as bag:
        caster.cast(CFG, "Dune", "http://x", device="1.2.3.4")
    assert all(n.code != "receiver_app_ignored" for n in bag)


def test_cast_falls_back_to_catt_when_bridge_never_starts(monkeypatch):
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: True)

    def fake_load(ip, url, *, follow=True, **meta):
        yield {"kind": "failed", "error": "bridge_unavailable", "message": "x"}

    monkeypatch.setattr(caster.bridge, "cast_load", fake_load)
    called = {}

    def fake_catt(*a, **k):
        called["catt"] = True
        # Mirrors the real `_cast_via_catt` contract. The old stub returned a 3-tuple the
        # real one never produced, and the assertion below pinned the resulting 4-tuple —
        # stub drift that a bare-tuple contract cannot catch (ADR 0031).
        return cast_delivery.CastResult(1.0, 2.0, started=True)

    monkeypatch.setattr(caster, "_cast_via_catt", fake_catt)
    r = caster.cast(CFG, "Dune", "http://x", device="1.2.3.4")
    assert (r.pos, r.dur, r.started) == (1.0, 2.0, True)
    assert called.get("catt") is True


def test_cast_uses_catt_when_bridge_absent(monkeypatch):
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    called = {}

    def fake_catt(*a, **k):
        called["catt"] = True
        return cast_delivery.CastResult(0.0, 0.0, started=True)

    monkeypatch.setattr(caster, "_cast_via_catt", fake_catt)
    caster.cast(CFG, "Dune", "http://x", device="1.2.3.4")
    assert called.get("catt") is True


def test_fire_and_return_reports_started(monkeypatch):
    """The mirror image of the failure pins: a handoff catt accepted (rc 0) counts as
    started even though no position was ever observed — otherwise every headless
    fire-and-return cast would report itself failed (ADR 0031)."""
    _cast_run(monkeypatch)
    r = caster.cast(CFG, "M", "http://u", device="TV", follow=False)
    assert (r.pos, r.dur) == (0.0, 0.0)
    assert r.started is True and r.error is None


def test_status_asks_the_receiver_once(monkeypatch):
    # P5: an unreachable TV used to cost two `catt info` timeouts per --status/--stop.
    calls = []
    monkeypatch.setattr(caster, "receiver_info", lambda dev: calls.append(dev) or {})
    monkeypatch.setattr(caster, "_bridge_track_info", lambda dev: ([], None))
    st = caster.status("10.0.0.9")
    assert calls == ["10.0.0.9"] and st["player_state"] == "IDLE" and st["volume"] is None
    assert st["volume_percent"] is None
    assert st["volume_control_type"] is None and st["app_id"] is None
    assert st["content_type"] is None and st["stream_type"] is None
    assert st["volume_step_interval"] is None


def test_headless_bridge_ctrl_c_aborts_without_catt_fallback(monkeypatch):
    """Fire-and-return Ctrl-C during the LOAD is a user abort: it re-raises (no catt
    re-cast of what was just cancelled) and reaps the detached subtitle server."""
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(caster.bridge, "stop", lambda device: None)

    def fake_load(ip, url, *, follow=True, **meta):
        raise KeyboardInterrupt
        yield {}

    monkeypatch.setattr(caster.bridge, "cast_load", fake_load)
    monkeypatch.setattr(caster, "_cast_via_catt", lambda *a, **k: pytest.fail("no fallback"))
    reaped = []
    monkeypatch.setattr(caster.serve, "reap_sub_server", lambda: reaped.append(1) or True)
    with pytest.raises(KeyboardInterrupt):
        caster.cast(CFG, "Dune", "http://x", device="1.2.3.4", follow=False)
    assert reaped


def test_bridge_subs_delivered_reads_receiver_confirmation(monkeypatch, tmp_path):
    """`subs_delivered` follows the receiver's activeTrackIds, not the intent: a caption
    track the TV did not activate is not reported as delivered."""
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(caster.serve, "lan_ip", lambda device: "127.0.0.1")
    monkeypatch.setattr(caster.serve, "ensure_firewall", lambda ip: None)
    sub = tmp_path / "s.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nciao\n")
    seen: dict = {}

    def run(tracks):
        def fake_load(ip, url, *, follow=True, **meta):
            seen.update(meta)
            yield {"kind": "started", "title": "Dune", "tracks": tracks}
            yield {"kind": "ended", "position": 1.0, "duration": 1.0}

        monkeypatch.setattr(caster.bridge, "cast_load", fake_load)
        return caster.cast(
            CFG, "Dune", "http://x", device="1.2.3.4", sub_paths=(str(sub),), sub_lang="ita"
        )

    assert run([1]).subs_delivered is True
    assert seen["subtitle_lang"] == "it" and seen["subtitle_name"] == "Italiano"
    assert run([2]).subs_delivered is False


def test_catt_gets_the_cleaned_webvtt(tmp_path):
    sub = tmp_path / "s.srt"
    sub.write_bytes("1\n00:00:00,000 --> 00:00:01,000\n{\\an8}\x93ciao\x94\n".encode("latin-1"))
    out = caster.catt_sub(str(sub))
    assert out.endswith(".vtt")
    assert "“ciao”" in Path(out).read_text(encoding="utf-8")
    assert caster.catt_sub("/nonexistent.srt") == "/nonexistent.srt"


def test_status_reports_the_probed_duration_on_a_live_cast(monkeypatch):
    """A growing live playlist reports duration -1 (history rejects it): status takes the
    probed runtime (ADR 0039). Positions are already film time (the served playlist
    starts with a gap covering what the producer skipped)."""
    monkeypatch.setattr(
        caster, "receiver_info",
        lambda d: {"player_state": "PLAYING", "current_time": 3100.0, "duration": -1.0},
    )  # fmt: skip
    monkeypatch.setattr(caster, "_bridge_track_info", lambda d: ([], None))
    monkeypatch.setattr(caster.cast_delivery, "live_duration", lambda d: 6472.0)
    st = caster.status("10.0.0.5")
    assert st["position"] == 3100.0 and st["duration"] == 6472.0


# --- catt 0.13 LOAD / MediaInformation (ADR 0050) ---------------------------


def _fixture_media(name: str) -> dict:
    raw = json.loads((_DATA / name).read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def test_catt_media_info_remux_file_matches_fixture():
    """catt CLI remux/file fallback LOAD: title + BUFFERED + video/mp4; no images."""
    got = caster.catt_cli_media_info("The Nice Guys")
    assert got == _fixture_media("catt_load_mediainfo_remux.json")
    assert "images" not in got["metadata"] and "thumb" not in got["metadata"]
    assert "contentId" not in got  # never a debrid / LAN URL in the fixture contract


def test_catt_lib_media_info_remux_includes_images():
    """Library LOAD (the shipped catt path): title + images[0].url + BUFFERED + mp4."""
    poster = "https://images.metahub.space/poster/medium/tt7068946/img"
    meta = caster.CastMeta(poster=poster, content_type="video/mp4")
    got = caster.catt_lib_media_info("The Nice Guys", meta)
    assert got == _fixture_media("catt_load_mediainfo_with_poster.json")
    assert got["metadata"]["images"][0]["url"] == poster
    assert got["metadata"]["metadataType"] == caster.CATT_METADATA_MOVIE
    assert got["contentType"] == "video/mp4" and got["streamType"] == "BUFFERED"


def test_catt_play_kwargs_one_load_with_thumb():
    poster = "https://images.metahub.space/poster/medium/tt7068946/img"
    meta = caster.CastMeta(poster=poster, content_type="video/mp4")
    kw = caster.catt_play_kwargs("The Nice Guys", meta, content_type="video/mp4")
    assert kw["title"] == "The Nice Guys"
    assert kw["thumb"] == poster
    assert kw["content_type"] == "video/mp4"
    assert kw["stream_type"] == "BUFFERED"
    assert kw["media_info"]["metadata"]["metadataType"] == caster.CATT_METADATA_MOVIE
    assert kw["media_info"]["metadata"]["images"][0]["url"] == poster
    assert kw["media_info"]["metadata"]["title"] == "The Nice Guys"


def _pychromecast_14_0_1_load_media(url: str, **kw) -> dict:
    """Replica of pychromecast 14.0.1 `MediaController._send_start_play_media`
    (pychromecast/controllers/media.py:475-493). catt 0.13.3 pins
    `pychromecast>=14.0.1,<15` and forwards `thumb=` + `media_info=`
    (`catt/controllers.py:597-608`)."""
    return caster.catt_load_media(url, **kw)


def test_catt_lib_load_payload_at_pychromecast_boundary():
    """Exact LOAD media at the pychromecast 14.0.1 boundary: type 1, title, images[0]."""
    poster = "https://images.metahub.space/poster/medium/tt7068946/img"
    kw = caster.catt_play_kwargs(
        "The Nice Guys", caster.CastMeta(poster=poster, content_type="video/mp4")
    )
    # nstream's media_info already carries images (not thumb-only).
    assert kw["media_info"]["metadata"]["images"][0]["url"] == poster
    media = _pychromecast_14_0_1_load_media("http://192.168.1.10:45000/cast/tok/stream.mp4", **kw)
    assert media["metadata"]["metadataType"] == caster.CATT_METADATA_MOVIE
    assert media["metadata"]["title"] == "The Nice Guys"
    assert media["metadata"]["images"][0]["url"] == poster
    assert media["contentType"] == "video/mp4"
    assert media["streamType"] == "BUFFERED"


def test_catt_play_kwargs_has_no_volume_or_mute():
    """Library LOAD does not SET_VOLUME / unmute; muted flip is receiver-side."""
    kw = caster.catt_play_kwargs(
        "The Nice Guys",
        caster.CastMeta(poster="https://images.metahub.space/poster/medium/tt1/img"),
    )
    banned = {"volume", "muted", "unmute", "volume_muted", "volume_level"}
    assert banned.isdisjoint(kw)
    assert banned.isdisjoint(kw.get("media_info") or {})
    assert banned.isdisjoint((kw.get("media_info") or {}).get("metadata") or {})


def test_catt_play_kwargs_tvshow_metadata_type():
    meta = caster.CastMeta(
        poster="https://images.metahub.space/poster/medium/tt4236770/img",
        series_title="The Boys",
        season=2,
        episode=5,
    )
    kw = caster.catt_play_kwargs("Good for the Soul", meta)
    assert kw["media_info"]["metadata"]["metadataType"] == caster.CATT_METADATA_TVSHOW
    assert kw["media_info"]["metadata"]["seriesTitle"] == "The Boys"
    assert kw["thumb"] == "https://images.metahub.space/poster/medium/tt4236770/img"
    assert kw["media_info"]["metadata"]["images"][0]["url"] == kw["thumb"]
    body = caster.catt_lib_media_info("Good for the Soul", meta)
    assert body["metadata"]["images"][0]["url"] == kw["thumb"]
    assert body["metadata"]["metadataType"] == 2


def test_catt_poster_url_cinemeta_metahub_only():
    assert caster.catt_poster_url("https://images.metahub.space/poster/medium/tt1/img")
    assert caster.catt_poster_url("https://v3-cinemeta.strem.io/poster.jpg")
    assert caster.catt_poster_url("https://images.example.test/poster/tt7068946.jpg") == ""
    assert caster.catt_poster_url("https://debrid.example/p.jpg") == ""
    assert caster.catt_poster_url("http://192.168.1.10/p.jpg") == ""
    assert caster.catt_poster_url("http://10.0.0.5/cast/tok/poster.jpg") == ""
    assert caster.catt_poster_url("") == ""


def test_catt_jpeg_poster_url_rewrites_small_webp_to_medium():
    small = "https://images.metahub.space/poster/small/tt6263850/img"
    medium = "https://images.metahub.space/poster/medium/tt6263850/img"
    assert caster.catt_jpeg_poster_url(small) == medium
    assert caster.catt_jpeg_poster_url(medium) == medium
    assert (
        caster.catt_jpeg_poster_url("https://images.metahub.space/poster/small/tt1/img.webp") == ""
    )
    assert caster.catt_jpeg_poster_url("https://debrid.example/p.jpg") == ""


def test_catt_image_url_accepts_lan_poster_jpg():
    lan = "http://192.168.1.103:45000/cast/toktest/poster.jpg"
    assert caster.catt_image_url(lan) == lan
    assert caster.catt_image_url("http://192.168.1.103:45000/cast/tok/other.jpg") == ""
    assert caster.catt_image_url("http://192.168.1.103/p.jpg") == ""
    small = "https://images.metahub.space/poster/small/tt6263850/img"
    assert (
        caster.catt_image_url(small) == "https://images.metahub.space/poster/medium/tt6263850/img"
    )


def test_catt_lib_load_debug_redacts_token(caplog):
    """`--debug` LOAD dump keeps the path, never the capability token."""
    import logging

    caplog.set_level(logging.DEBUG, logger="nstream.cast")
    url = "http://192.168.1.103:45000/cast/s3cretTok/stream.mp4"
    poster = "http://192.168.1.103:45000/cast/s3cretTok/poster.jpg"
    load = caster.catt_play_kwargs("Deadpool & Wolverine", caster.CastMeta(poster=poster))
    caster._log_catt_load(url, load)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "catt lib LOAD media=" in text
    assert "s3cretTok" not in text
    assert "/cast/<token>/poster.jpg" in text
    assert "/cast/<token>/stream.mp4" in text
    assert "metadataType" in text


def test_catt_load_helper_play_media_url_kwargs(monkeypatch):
    """_catt_load.py (catt's interpreter) forwards title/thumb/BUFFERED/mp4 to play_media_url."""
    seen: dict = {}

    class _Ctrl:
        def prep_app(self):
            seen["prep"] = True

        def play_media_url(self, url, **kw):
            seen["url"] = url
            seen["kw"] = kw

    class _Dev:
        def __init__(self, ip_addr=""):
            seen["ip"] = ip_addr

        @property
        def controller(self):
            return _Ctrl()

    monkeypatch.setattr(
        _catt_load.importlib, "import_module", lambda name: types.SimpleNamespace(CattDevice=_Dev)
    )
    payload = {
        "ip": "10.0.0.5",
        "url": "http://192.168.1.10:45000/cast/tok/stream.mp4",
        "title": "The Nice Guys",
        "thumb": "https://images.metahub.space/poster/medium/tt7068946/img",
        "content_type": "video/mp4",
        "stream_type": "BUFFERED",
        "media_info": {"metadata": {"metadataType": 1}},
    }
    monkeypatch.setattr(_catt_load.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _catt_load.main() == 0
    assert seen["ip"] == "10.0.0.5" and seen["prep"] is True
    assert seen["kw"]["title"] == "The Nice Guys"
    assert seen["kw"]["thumb"] == payload["thumb"]
    assert seen["kw"]["content_type"] == "video/mp4"
    assert seen["kw"]["stream_type"] == "BUFFERED"
    assert seen["kw"]["media_info"]["metadata"]["metadataType"] == 1


def test_catt_lib_play_inprocess_play_media_url(monkeypatch):
    """In-process catt.api: one play_media_url LOAD with thumb + metadataType 1."""
    seen: dict = {}

    class _Ctrl:
        def prep_app(self):
            seen["prep"] = True

        def play_media_url(self, url, **kw):
            seen["url"] = url
            seen["kw"] = kw

    class _Dev:
        def __init__(self, **kw):
            seen["ctor"] = kw

        @property
        def controller(self):
            return _Ctrl()

    monkeypatch.setattr(caster, "_catt_device_cls", lambda: _Dev)
    poster = "https://images.metahub.space/poster/medium/tt7068946/img"
    assert caster.catt_lib_play(
        "10.0.0.5",
        "http://192.168.1.10:45000/cast/tok/stream.mp4",
        title="The Nice Guys",
        meta=caster.CastMeta(poster=poster, content_type="video/mp4"),
    )
    assert seen["ctor"] == {"ip_addr": "10.0.0.5"} and seen["prep"] is True
    assert seen["kw"]["title"] == "The Nice Guys"
    assert seen["kw"]["thumb"] == poster
    assert seen["kw"]["content_type"] == "video/mp4"
    assert seen["kw"]["stream_type"] == "BUFFERED"
    assert seen["kw"]["media_info"]["metadata"]["metadataType"] == caster.CATT_METADATA_MOVIE


def test_catt_play_kwargs_drops_non_https_poster():
    kw = caster.catt_play_kwargs("T", caster.CastMeta(poster="http://10.0.0.5/cast/tok/p.jpg"))
    assert "thumb" not in kw
    assert "images" not in kw["media_info"]["metadata"]


def test_catt_lib_play_used_before_cli(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(caster, "catt_can_lib_load", lambda: True)

    def fake_play(ip, url, **kw):
        seen.update(ip=ip, url=url, **kw)
        return caster.CATT_LIB_OK

    monkeypatch.setattr(caster, "catt_lib_outcome", fake_play)
    calls = _cast_run(monkeypatch)
    poster = "https://images.metahub.space/poster/medium/tt7068946/img"
    caster.cast(
        CFG,
        "The Nice Guys",
        "http://u",
        device="10.0.0.5",
        follow=False,
        meta=caster.CastMeta(poster=poster, content_type="video/mp4"),
    )
    assert seen["ip"] == "10.0.0.5" and seen["url"] == "http://u"
    assert seen["title"] == "The Nice Guys" and seen["meta"].poster == poster
    assert all("cast" not in c or "info" in c for c in calls)


def test_catt_cast_argv_title_and_buffered(monkeypatch):
    calls = _cast_run(monkeypatch)
    meta = caster.CastMeta(
        poster="https://images.metahub.space/poster/medium/tt7068946/img",
        content_type="video/mp4",
    )
    caster.cast(CFG, "The Nice Guys", "http://u", device="TV", follow=False, meta=meta)
    launch = calls[0]
    assert launch[launch.index("-l") + 1] == "The Nice Guys"
    assert launch[launch.index("--stream-type") + 1] == "BUFFERED"
    assert meta.poster not in launch
    assert "--thumb" not in launch


def test_catt_display_title_series():
    meta = caster.CastMeta(series_title="The Boys", season=2, episode=5)
    assert caster.catt_display_title("Good for the Soul", meta) == (
        "The Boys · S02E05 · Good for the Soul"
    )
    assert caster.catt_display_title("", meta) == "The Boys · S02E05"
    assert caster.catt_display_title("Dune", None) == "Dune"


def test_catt_cast_argv_omits_empty_title():
    args = caster.catt_cast_argv("1.2.3.4", "/tmp/cast-x.mp4", title="  ")
    assert "-l" not in args
    assert args[-2:] == ["--stream-type", "BUFFERED"]


def test_catt_cast_argv_omits_meta_flags_before_0_13_2(monkeypatch):
    """catt 0.13.0/0.13.1: click rejects `-l`; keep the pre-0050 argv."""
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: False)
    args = caster.catt_cast_argv("TV", "http://u", title="The Nice Guys")
    assert "-l" not in args and "--stream-type" not in args
    assert args == ["catt", "-d", "TV", "cast", "http://u"]


def test_catt_can_lib_load_requires_0_13_2(monkeypatch):
    monkeypatch.setattr(caster, "catt_inprocess_supports_load_meta", lambda: False)
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: object)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: "/usr/bin/python")
    assert caster.catt_can_lib_load() is False


def test_catt_inprocess_version_reads_imported(monkeypatch):
    """In-process gate uses imported catt.__version__, not `catt --version` on PATH."""
    monkeypatch.setattr(
        caster.importlib,
        "import_module",
        lambda name: types.SimpleNamespace(__version__="0.13.1") if name == "catt" else None,
    )
    assert caster.catt_inprocess_version() == (0, 13, 1)
    monkeypatch.setattr(caster, "catt_inprocess_supports_load_meta", lambda: False)
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: True)
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: object)
    assert caster.catt_can_lib_load() is False


def test_parse_catt_version():
    assert caster._parse_catt_version("catt v0.13.1, codename") == (0, 13, 1)
    assert caster._parse_catt_version("0.13.3") == (0, 13, 3)
    assert caster._parse_catt_version("nope") is None
    assert (0, 13, 1) < caster.CATT_LOAD_META_MIN <= (0, 13, 2)


def test_cast_retries_cli_without_meta_flags_on_click_usage(monkeypatch):
    launches: list[list[str]] = []

    class _P:
        def __init__(self, rc, err=""):
            self.returncode = rc
            self.stdout = ""
            self.stderr = err

    def fake(cmd, **k):
        launches.append(list(cmd))
        if "cast" in cmd and "-l" in cmd:
            return _P(2, "Error: No such option: -l")
        return _P(0)

    monkeypatch.setattr(caster.subprocess, "run", fake)
    r = caster.cast(CFG, "The Nice Guys", "http://u", device="TV", follow=False)
    assert r.started is True
    assert any("-l" in c for c in launches)
    retry = [c for c in launches if "cast" in c and "-l" not in c]
    assert retry and "--stream-type" not in retry[0]


def test_catt_device_ctor_kwargs_name_vs_ip():
    assert caster.catt_device_ctor_kwargs("10.0.0.5") == {"ip_addr": "10.0.0.5"}
    assert caster.catt_device_ctor_kwargs("2001:db8::1") == {"ip_addr": "2001:db8::1"}
    assert caster.catt_device_ctor_kwargs("43PUS9235/12") == {"name": "43PUS9235/12"}
    assert caster.catt_device_ctor_kwargs("Salotto") == {"name": "Salotto"}


def test_catt_lib_play_uses_name_for_friendly_device(monkeypatch):
    seen: dict = {}

    class _Ctrl:
        def prep_app(self):
            return None

        def play_media_url(self, url, **kw):
            seen["url"] = url

    class _Dev:
        def __init__(self, **kw):
            seen["ctor"] = kw

        @property
        def controller(self):
            return _Ctrl()

    monkeypatch.setattr(caster, "_catt_device_cls", lambda: _Dev)
    assert caster.catt_lib_play("43PUS9235/12", "http://u", title="T")
    assert seen["ctor"] == {"name": "43PUS9235/12"}


def test_catt_load_helper_name_ctor(monkeypatch):
    seen: dict = {}

    class _Ctrl:
        def prep_app(self):
            return None

        def play_media_url(self, url, **kw):
            seen["url"] = url

    class _Dev:
        def __init__(self, **kw):
            seen["ctor"] = kw

        @property
        def controller(self):
            return _Ctrl()

    monkeypatch.setattr(
        _catt_load.importlib, "import_module", lambda name: types.SimpleNamespace(CattDevice=_Dev)
    )
    payload = {"name": "43PUS9235/12", "url": "http://u", "title": "T"}
    monkeypatch.setattr(_catt_load.sys, "stdin", io.StringIO(json.dumps(payload)))
    assert _catt_load.main() == 0
    assert seen["ctor"] == {"name": "43PUS9235/12"}


class CastError(Exception):
    """Name-matches catt.error.CastError for `_is_catt_session_wait`."""


_CATT_SESSION_WAIT = "Waiting for the media session to become active timed out after 30 seconds"


def _mc_controller(*, after=None, before=None, hang=False):
    """play_media_url stand-in with `_controller.play_media` for the sent hook."""

    class _MC:
        def play_media(self, *a, **k):
            return None

    class _Ctrl:
        def __init__(self):
            self._controller = _MC()

        def prep_app(self):
            return None

        def play_media_url(self, *a, **k):
            if before is not None:
                raise before
            self._controller.play_media(*a, **k)
            if hang:
                time.sleep(30)
            if after is not None:
                raise after

    class _Dev:
        def __init__(self, **kw):
            self._ctrl = _Ctrl()

        @property
        def controller(self):
            return self._ctrl

    return _Dev


def test_catt_load_helper_session_wait_is_rc4(monkeypatch):
    class _MC:
        def play_media(self, *a, **k):
            return None

    class _Ctrl:
        def __init__(self):
            self._controller = _MC()

        def prep_app(self):
            return None

        def play_media_url(self, url, **kw):
            self._controller.play_media(url, kw.get("content_type"), **kw)
            raise CastError(_CATT_SESSION_WAIT)

    class _Dev:
        def __init__(self, **kw):
            self._ctrl = _Ctrl()

        @property
        def controller(self):
            return self._ctrl

    monkeypatch.setattr(
        _catt_load.importlib, "import_module", lambda name: types.SimpleNamespace(CattDevice=_Dev)
    )
    monkeypatch.setattr(
        _catt_load.sys, "stdin", io.StringIO(json.dumps({"ip": "10.0.0.5", "url": "http://u"}))
    )
    assert _catt_load.main() == 4


def test_catt_load_helper_before_load_is_rc1(monkeypatch):
    class _Ctrl:
        def prep_app(self):
            return None

        def play_media_url(self, url, **kw):
            raise TypeError("unexpected keyword argument")

    class _Dev:
        def __init__(self, **kw):
            self._ctrl = _Ctrl()

        @property
        def controller(self):
            return self._ctrl

    monkeypatch.setattr(
        _catt_load.importlib, "import_module", lambda name: types.SimpleNamespace(CattDevice=_Dev)
    )
    monkeypatch.setattr(
        _catt_load.sys, "stdin", io.StringIO(json.dumps({"ip": "10.0.0.5", "url": "http://u"}))
    )
    assert _catt_load.main() == 1


def test_catt_lib_abandoned_skips_late_play_media(monkeypatch):
    """Timeout before sent: a late play_media must not LOAD on top of CLI."""
    sent = {"n": 0}

    class _MC:
        def play_media(self, *a, **k):
            sent["n"] += 1

    class _Ctrl:
        def __init__(self):
            self._controller = _MC()

        def prep_app(self):
            time.sleep(30)

        def play_media_url(self, *a, **k):
            self._controller.play_media(*a, **k)

    class _Dev:
        def __init__(self, **kw):
            self._ctrl = _Ctrl()

        @property
        def controller(self):
            return self._ctrl

    monkeypatch.setattr(caster, "_catt_device_cls", lambda: _Dev)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: None)
    monkeypatch.setattr(caster.util, "CATT_LIB_LOAD_TIMEOUT", 0.05)
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.0)
    monkeypatch.setattr(caster, "receiver_info", lambda dev: {})
    assert caster.catt_lib_play("10.0.0.5", "http://u", title="T") is False
    time.sleep(0.2)
    assert sent["n"] == 0


def test_catt_lib_play_inprocess_times_out(monkeypatch):
    class _Dev:
        def __init__(self, **kw):
            time.sleep(30)

        @property
        def controller(self):
            raise AssertionError("must not reach play")

    monkeypatch.setattr(caster, "_catt_device_cls", lambda: _Dev)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: None)
    monkeypatch.setattr(caster.util, "CATT_LIB_LOAD_TIMEOUT", 0.05)
    monkeypatch.setattr(caster, "receiver_info", lambda dev: {})
    t0 = time.monotonic()
    assert caster.catt_lib_play("10.0.0.5", "http://u", title="T") is False
    assert time.monotonic() - t0 < 2.0


def test_catt_helper_timeout_receiver_playing_is_loaded(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: None)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: "/usr/bin/python")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["python"], timeout=0.05)

    monkeypatch.setattr(caster.subprocess, "run", boom)
    monkeypatch.setattr(
        caster, "receiver_info", lambda dev: {"player_state": "BUFFERING", "content_id": url}
    )
    assert caster.catt_lib_play("10.0.0.5", url, title="T") is True


def test_catt_lib_play_timeout_receiver_playing_is_loaded(monkeypatch):
    """Late play_media_url after play_media returned: TV has the LOAD → success."""
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: _mc_controller(hang=True))
    monkeypatch.setattr(caster.util, "CATT_LIB_LOAD_TIMEOUT", 0.05)
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.0)
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: {"player_state": "PLAYING", "content_id": url},
    )
    assert caster.catt_lib_play("10.0.0.5", url, title="T") is True


def test_catt_receiver_has_load_matches_path(monkeypatch):
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: {
            "player_state": "BUFFERING",
            "content_id": "http://192.168.1.10:45000/cast/tok/stream.mp4",
        },
    )
    assert caster.catt_receiver_has_load("10.0.0.5", "http://10.0.0.1:45000/cast/tok/stream.mp4")
    assert not caster.catt_receiver_has_load("10.0.0.5", "http://other/cast/other/x.mp4")


def test_cast_lib_timeout_confirmed_skips_cli(monkeypatch):
    """Direct cast: late LOAD after play_media + receiver playing → no second catt cast."""
    url = "http://u/stream.mp4"
    monkeypatch.setattr(caster, "catt_can_lib_load", lambda: True)
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: _mc_controller(hang=True))
    monkeypatch.setattr(caster.util, "CATT_LIB_LOAD_TIMEOUT", 0.05)
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.0)
    monkeypatch.setattr(
        caster, "receiver_info", lambda dev: {"player_state": "PLAYING", "content_id": url}
    )
    calls = _cast_run(monkeypatch)
    r = caster.cast(CFG, "T", url, device="10.0.0.5", follow=False)
    assert r.started is True
    assert not [c for c in calls if "cast" in c and url in c]


def test_catt_lib_play_session_wait_raise_is_unconfirmed(monkeypatch):
    """Post-LOAD session-wait CastError + empty receiver → unconfirmed, not success."""
    monkeypatch.setattr(
        caster, "_catt_device_cls", lambda: _mc_controller(after=CastError(_CATT_SESSION_WAIT))
    )
    monkeypatch.setattr(caster, "receiver_info", lambda dev: {})
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.0)
    poster = "https://images.metahub.space/poster/medium/tt7068946/img"
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    assert (
        caster.catt_lib_outcome(
            "10.0.0.5",
            url,
            title="The Nice Guys",
            meta=caster.CastMeta(poster=poster, content_type="video/mp4"),
        )
        == caster.CATT_LIB_UNCONFIRMED
    )
    assert (
        caster.catt_lib_play(
            "10.0.0.5",
            url,
            title="The Nice Guys",
            meta=caster.CastMeta(poster=poster, content_type="video/mp4"),
        )
        is False
    )


def test_catt_lib_session_wait_without_hook_is_unconfirmed(monkeypatch):
    """catt 0.13.3 CastError text is enough when `_controller` is missing."""

    class _Ctrl:
        def prep_app(self):
            return None

        def play_media_url(self, *a, **k):
            raise CastError(_CATT_SESSION_WAIT)

    class _Dev:
        def __init__(self, **kw):
            return None

        @property
        def controller(self):
            return _Ctrl()

    monkeypatch.setattr(caster, "_catt_device_cls", lambda: _Dev)
    monkeypatch.setattr(caster, "receiver_info", lambda dev: {})
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.0)
    assert caster.catt_lib_outcome("10.0.0.5", "http://u", title="T") == caster.CATT_LIB_UNCONFIRMED


def test_catt_lib_raise_before_load_is_fail(monkeypatch):
    monkeypatch.setattr(
        caster, "_catt_device_cls", lambda: _mc_controller(before=TypeError("bad kwargs"))
    )
    monkeypatch.setattr(caster, "receiver_info", lambda dev: {})
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.0)
    assert caster.catt_lib_outcome("10.0.0.5", "http://u", title="T") == caster.CATT_LIB_FAIL
    assert caster.catt_lib_play("10.0.0.5", "http://u", title="T") is False


def test_catt_lib_session_wait_later_receiver_ok(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    infos = iter([{}, {"player_state": "PLAYING", "content_id": url}])
    monkeypatch.setattr(
        caster, "_catt_device_cls", lambda: _mc_controller(after=CastError(_CATT_SESSION_WAIT))
    )
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: next(infos, {"player_state": "PLAYING", "content_id": url}),
    )
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.3)
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_POLL", 0.01)
    assert caster.catt_lib_play("10.0.0.5", url, title="T") is True


def test_catt_lib_session_wait_load_failed(monkeypatch):
    monkeypatch.setattr(
        caster, "_catt_device_cls", lambda: _mc_controller(after=CastError(_CATT_SESSION_WAIT))
    )
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: {"player_state": "IDLE", "idle_reason": "ERROR", "error": "LOAD_FAILED"},
    )
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.0)
    assert caster.catt_lib_outcome("10.0.0.5", "http://u", title="T") == caster.CATT_LIB_FAIL
    assert caster.catt_lib_play("10.0.0.5", "http://u", title="T") is False


def test_catt_receiver_load_state_failed_and_ok(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(
        caster, "receiver_info", lambda dev: {"player_state": "IDLE", "idleReason": "LOAD_FAILED"}
    )
    assert caster.catt_receiver_load_state("10.0.0.5", url) == caster.CATT_LIB_FAIL
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: {"player_state": "BUFFERING", "content_id": url},
    )
    assert caster.catt_receiver_load_state("10.0.0.5", url) == caster.CATT_LIB_OK
    monkeypatch.setattr(caster, "receiver_info", lambda dev: {})
    assert caster.catt_receiver_load_state("10.0.0.5", url) == caster.CATT_LIB_UNCONFIRMED


def test_catt_receiver_interrupted_foreign_content_is_unconfirmed(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: {
            "player_state": "IDLE",
            "idle_reason": "INTERRUPTED",
            "content_id": "http://other/cast/old/x.mp4",
        },
    )
    assert caster.catt_receiver_load_state("10.0.0.5", url) == caster.CATT_LIB_UNCONFIRMED


def test_catt_receiver_error_foreign_content_is_unconfirmed(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: {
            "player_state": "IDLE",
            "idle_reason": "ERROR",
            "content_id": "http://other/cast/old/x.mp4",
        },
    )
    assert caster.catt_receiver_load_state("10.0.0.5", url) == caster.CATT_LIB_UNCONFIRMED


def test_catt_receiver_load_failed_our_content_is_fail(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: {
            "player_state": "IDLE",
            "idleReason": "LOAD_FAILED",
            "content_id": url,
        },
    )
    assert caster.catt_receiver_load_state("10.0.0.5", url) == caster.CATT_LIB_FAIL


def test_catt_helper_rc1_never_sent(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: None)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: "/usr/bin/python")
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: True)

    class _P:
        returncode = 1
        stdout = ""
        stderr = "catt-load: play_media_url failed"

    monkeypatch.setattr(caster.subprocess, "run", lambda *a, **k: _P())
    monkeypatch.setattr(caster, "receiver_info", lambda dev: {})
    assert caster.catt_lib_outcome("10.0.0.5", url, title="T") == caster.CATT_LIB_FAIL
    assert caster.catt_lib_play("10.0.0.5", url, title="T") is False


def test_catt_helper_rc4_unconfirmed(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: None)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: "/usr/bin/python")
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: True)
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.0)

    class _P:
        returncode = 4
        stdout = ""
        stderr = "catt-load: media session unconfirmed"

    monkeypatch.setattr(caster.subprocess, "run", lambda *a, **k: _P())
    monkeypatch.setattr(caster, "receiver_info", lambda dev: {})
    assert caster.catt_lib_outcome("10.0.0.5", url, title="T") == caster.CATT_LIB_UNCONFIRMED
    assert caster.catt_lib_play("10.0.0.5", url, title="T") is False


def test_catt_helper_rc4_later_receiver_ok(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: None)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: "/usr/bin/python")
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: True)
    infos = iter([{}, {"player_state": "PLAYING", "content_id": url}])

    class _P:
        returncode = 4
        stdout = ""
        stderr = "catt-load: media session unconfirmed"

    monkeypatch.setattr(caster.subprocess, "run", lambda *a, **k: _P())
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: next(infos, {"player_state": "PLAYING", "content_id": url}),
    )
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.3)
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_POLL", 0.01)
    assert caster.catt_lib_play("10.0.0.5", url, title="T") is True


def test_catt_helper_rc4_load_failed(monkeypatch):
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: None)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: "/usr/bin/python")
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: True)
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", 0.0)

    class _P:
        returncode = 4
        stdout = ""
        stderr = "catt-load: media session unconfirmed"

    monkeypatch.setattr(caster.subprocess, "run", lambda *a, **k: _P())
    monkeypatch.setattr(
        caster,
        "receiver_info",
        lambda dev: {"player_state": "IDLE", "idle_reason": "ERROR", "error": "LOAD_FAILED"},
    )
    assert caster.catt_lib_outcome("10.0.0.5", url, title="T") == caster.CATT_LIB_FAIL


def test_catt_helper_rc2_never_sent(monkeypatch):
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: None)
    monkeypatch.setattr(caster, "_catt_interpreter", lambda: "/usr/bin/python")
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: True)

    class _P:
        returncode = 2
        stdout = ""
        stderr = "catt-load: url required"

    monkeypatch.setattr(caster.subprocess, "run", lambda *a, **k: _P())
    assert caster.catt_lib_play("10.0.0.5", "http://u", title="T") is False


def test_catt_lib_play_false_before_0_13_2(monkeypatch):
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: False)
    monkeypatch.setattr(caster, "catt_inprocess_supports_load_meta", lambda: False)
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: None)
    assert caster.catt_lib_play("10.0.0.5", "http://u", title="T") is False


def test_catt_unconfirmed_subtitle_reap_uses_skip_if(monkeypatch):
    """Unconfirmed leftover VTT server gets skip_if (+ idle_for), not a fixed clock."""
    scheduled: dict = {}

    def idle() -> float:
        return 0.0

    def shut() -> None:
        return None

    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(caster, "catt_can_lib_load", lambda: True)
    monkeypatch.setattr(caster, "catt_lib_outcome", lambda *a, **k: caster.CATT_LIB_UNCONFIRMED)
    monkeypatch.setattr(caster, "_catt_unconfirmed_notice", lambda: None)

    def fake_sub(vtt, device, sub_lang, follow, kwargs):
        kwargs[caster._SUB_IDLE_FOR] = idle
        return shut

    monkeypatch.setattr(caster, "_serve_subtitle", fake_sub)
    monkeypatch.setattr(caster.srt, "to_vtt", lambda p: "WEBVTT")
    monkeypatch.setattr(
        caster.serve,
        "schedule_reap",
        lambda fn, seconds, **k: scheduled.update(fn=fn, s=seconds, k=k),
    )
    r = caster._cast_via_catt(
        CFG, "T", "http://u", device="10.0.0.5", follow=False, sub_paths=("/tmp/x.srt",)
    )
    assert r.started is False and r.unconfirmed is True
    assert scheduled["fn"] is shut
    assert scheduled["s"] == caster.util.CATT_LIB_UNCONFIRMED_SERVE_S
    assert scheduled["k"]["handle"] is shut
    assert scheduled["k"]["idle_for"] is idle
    assert scheduled["k"]["skip_if"] is not None
