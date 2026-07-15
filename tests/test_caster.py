"""Unit tests for Chromecast playback via catt: device resolution, the cast() poll
loop (resume/auto-advance), and the in-cast audio-language switch."""

from __future__ import annotations

import subprocess

import pytest

from nstream import caster
from nstream.config import Config

CFG = Config(torrentio_base="tb")


# --- device resolution -----------------------------------------------------


@pytest.fixture(autouse=True)
def _catt_on_path(monkeypatch):
    """resolve_device guards on catt's presence; keep tests hermetic (the makepkg
    check() chroot, for one, has no catt installed)."""
    monkeypatch.setattr(caster.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")


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


# --- auto-cast confirmation --------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_confirm_latch():
    caster._cast_confirmed = False
    yield
    caster._cast_confirmed = False


def test_resolve_device_confirm_accepted(monkeypatch):
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("TV1", "10.0.0.9")]))
    monkeypatch.setattr(caster, "_confirm_device", lambda name, ip: True)
    assert caster.resolve_device(Config(torrentio_base="tb"), confirm=True) == "10.0.0.9"


def test_resolve_device_confirm_declined_raises(monkeypatch):
    # A declined confirmation must fall back like an absent device (local playback).
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("TV1", "10.0.0.9")]))
    monkeypatch.setattr(caster, "_confirm_device", lambda name, ip: False)
    with pytest.raises(caster.CastUnavailable, match="rifiutato"):
        caster.resolve_device(Config(torrentio_base="tb"), confirm=True)


def test_resolve_device_confirm_preferred_device(monkeypatch):
    monkeypatch.setattr(
        caster.discovery,
        "get_devices",
        _scan([("Salotto", "192.168.1.5"), ("Camera", "192.168.1.6")]),
    )
    asked = []
    monkeypatch.setattr(caster, "_confirm_device", lambda name, ip: not asked.append((name, ip)))
    cfg = Config(torrentio_base="tb", cast_device="Salotto")
    assert caster.resolve_device(cfg, confirm=True) == "192.168.1.5"
    assert asked == [("Salotto", "192.168.1.5")]


class _Tty:
    def isatty(self):
        return True


def test_confirm_device_default_yes_and_session_latch(monkeypatch):
    monkeypatch.setattr(caster.sys, "stdin", _Tty())
    monkeypatch.setattr(caster.sys, "stderr", _Tty())
    prompts = []

    def _yes(msg, **k):
        prompts.append(msg)
        return True

    monkeypatch.setattr(caster, "_confirm", _yes)
    assert caster._confirm_device("TV1", "10.0.0.9") is True  # Sì
    assert "TV1" in prompts[0] and "10.0.0.9" in prompts[0]
    # Latched: the next play (binge advance) must not re-ask.
    monkeypatch.setattr(caster, "_confirm", lambda *a, **k: pytest.fail("must not re-ask"))
    assert caster._confirm_device("TV1", "10.0.0.9") is True


def test_confirm_device_refusal_not_latched(monkeypatch):
    monkeypatch.setattr(caster.sys, "stdin", _Tty())
    monkeypatch.setattr(caster.sys, "stderr", _Tty())
    monkeypatch.setattr(caster, "_confirm", lambda *a, **k: False)
    assert caster._confirm_device("TV1", "10.0.0.9") is False
    assert caster._cast_confirmed is False  # a refusal is per-play, asked again next time


def test_confirm_device_non_tty_passes(monkeypatch):
    # Headless/piped callers must never block on confirm (no fzf).
    monkeypatch.setattr(caster, "_confirm", lambda *a, **k: pytest.fail("must not prompt"))
    assert caster._confirm_device("TV1", "10.0.0.9") is True


def test_resolve_device_multiple_prompts_ip(monkeypatch):
    monkeypatch.setattr(
        caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")])
    )
    monkeypatch.setattr(caster, "fzf", lambda items, prompt: "10.0.0.2")
    assert caster.resolve_device(Config(torrentio_base="tb")) == "10.0.0.2"


def test_resolve_device_choose_forces_picker_by_name(monkeypatch):
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1")]))
    seen = {}

    def fk(items, prompt):
        seen["items"] = items
        return "10.0.0.1"

    monkeypatch.setattr(caster, "fzf", fk)
    assert caster.resolve_device(Config(torrentio_base="tb"), choose=True) == "10.0.0.1"
    assert seen["items"] == [("TV1", "10.0.0.1")]  # label=name, value=ip


def test_resolve_device_none_raises(monkeypatch):
    # Empty scan → trust it (no fall-through to a stale cast-resolve default).
    monkeypatch.setattr(caster.discovery, "get_devices", _scan([]))
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(Config(torrentio_base="tb"))


def test_resolve_device_cancel_raises(monkeypatch):
    monkeypatch.setattr(
        caster.discovery, "get_devices", _scan([("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")])
    )
    monkeypatch.setattr(caster, "fzf", lambda items, prompt: None)
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(Config(torrentio_base="tb"))


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
        device="TV", start=125.0, sub_paths=("/tmp/x.srt",), next_label=None,
    )  # fmt: skip
    launch = calls[0]
    assert launch[:2] == ["catt", "-d"] and launch[2] == "TV"
    assert "cast" in launch and "http://u" in launch
    assert "-t" in launch and "125" in launch
    assert "-s" in launch and "/tmp/x.srt" in launch


def test_cast_tracks_position_and_advances_on_finish(monkeypatch):
    _cast_run(
        monkeypatch,
        info_seq=[
            {"player_state": "PLAYING", "current_time": 10.0, "duration": 100.0},
            {"player_state": "PLAYING", "current_time": 99.0, "duration": 100.0},
            {"player_state": "IDLE", "duration": 100.0},
        ],
    )
    pos, dur, advance, _subs = caster.cast(
        CFG, "Show E1", "http://u", device="TV", next_label="Show E2"
    )
    assert (pos, dur) == (99.0, 100.0)
    assert advance is True  # ended past _CAST_DONE with a next episode queued


def test_cast_no_advance_on_early_stop(monkeypatch):
    _cast_run(
        monkeypatch,
        info_seq=[
            {"player_state": "PLAYING", "current_time": 20.0, "duration": 100.0},
            {"player_state": "IDLE", "duration": 100.0},  # stopped at 20% → not finished
        ],
    )
    pos, dur, advance, _subs = caster.cast(
        CFG, "Show E1", "http://u", device="TV", next_label="Show E2"
    )
    assert (pos, dur) == (20.0, 100.0)
    assert advance is False


def test_cast_launch_failure_returns_zero(monkeypatch):
    _cast_run(monkeypatch, launch_rc=1)
    assert caster.cast(CFG, "M", "http://u", device="TV") == (0.0, 0.0, False, False)


def test_cast_gives_up_if_never_starts(monkeypatch):
    # Receiver stays idle/unreachable forever → bail after _CAST_GIVEUP polls,
    # never loops indefinitely.
    calls = _cast_run(monkeypatch, info_seq=[])  # every info poll fails
    assert caster.cast(CFG, "M", "http://u", device="TV") == (0.0, 0.0, False, False)
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
    assert result == (0.0, 0.0, False, False)
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
    assert caster.cast(CFG, "M", "http://u", device="TV") == (0.0, 0.0, False, False)
    assert len(polls) == caster._CAST_GIVEUP


def test_stop_and_volume_timeout_degrade(monkeypatch):
    def hang(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, k.get("timeout", 10))

    monkeypatch.setattr(caster.subprocess, "run", hang)
    assert caster.stop("1.2.3.4") is False
    assert caster.set_volume("1.2.3.4", 50) is False
    assert caster.status("1.2.3.4")["player_state"] == "IDLE"  # _raw_info degrades too


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
    keys = iter(["a", None, None, None, None])
    monkeypatch.setattr(caster, "_poll_wait", lambda _t: next(keys, None))
    monkeypatch.setattr(caster, "fzf", lambda items, prompt: "eng")
    caster.cast(
        CFG, "Film", "http://ita",
        device="TV", langs=("ita", "eng"), resolve_lang=lambda lang: "http://eng",
    )  # fmt: skip
    recasts = [c for c in calls if "cast" in c and "http://eng" in c]
    assert recasts and "-t" in recasts[0]  # re-cast the eng url with a seek


def test_cast_hotkey_esc_keeps_current(monkeypatch):
    calls = _cast_run(monkeypatch, info_seq=[{"player_state": "IDLE"}])
    keys = iter(["a", None, None, None, None])
    monkeypatch.setattr(caster, "_poll_wait", lambda _t: next(keys, None))
    monkeypatch.setattr(caster, "fzf", lambda items, prompt: None)  # ESC
    resolved = []
    caster.cast(
        CFG, "Film", "http://ita",
        device="TV", langs=("ita", "eng"), resolve_lang=lambda lang: resolved.append(lang),
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
    monkeypatch.setattr(caster, "fzf", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fzf")))
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(CFG, headless=True)


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


def test_status_normalizes(monkeypatch):
    info = {
        "player_state": "PLAYING",
        "current_time": 12.0,
        "duration": 100.0,
        "media_metadata": {"title": "The Matrix"},
        "volume_level": 0.4,
        "volume_muted": False,
    }

    class _R:
        returncode = 0
        stdout = __import__("json").dumps(info)

    monkeypatch.setattr(caster.subprocess, "run", lambda cmd, **k: _R())
    st = caster.status("1.2.3.4")
    assert st["player_state"] == "PLAYING" and st["title"] == "The Matrix"
    assert st["volume"] == 0.4 and st["muted"] is False


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
    pos, dur, advance, _subs = caster.cast(
        CFG,
        "Dune",
        "http://x",
        device="1.2.3.4",
        next_label="ep2",
        meta=caster.CastMeta(poster="p.jpg"),
        on_event=seen.append,
    )
    assert (pos, dur) == (98.0, 100.0)
    assert advance is True  # ended past _CAST_DONE with a queued next episode
    assert [e["kind"] for e in seen] == ["started", "playing", "ended"]


def test_cast_falls_back_to_catt_when_bridge_never_starts(monkeypatch):
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: True)

    def fake_load(ip, url, *, follow=True, **meta):
        yield {"kind": "failed", "error": "bridge_unavailable", "message": "x"}

    monkeypatch.setattr(caster.bridge, "cast_load", fake_load)
    called = {}

    def fake_catt(*a, **k):
        called["catt"] = True
        return (1.0, 2.0, False)

    monkeypatch.setattr(caster, "_cast_via_catt", fake_catt)
    assert caster.cast(CFG, "Dune", "http://x", device="1.2.3.4") == (1.0, 2.0, False, False)
    assert called.get("catt") is True


def test_cast_uses_catt_when_bridge_absent(monkeypatch):
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    called = {}

    def fake_catt(*a, **k):
        called["catt"] = True
        return (0.0, 0.0, False)

    monkeypatch.setattr(caster, "_cast_via_catt", fake_catt)
    caster.cast(CFG, "Dune", "http://x", device="1.2.3.4")
    assert called.get("catt") is True
