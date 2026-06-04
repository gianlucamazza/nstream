"""Unit tests for Chromecast playback via catt: device resolution, the cast() poll
loop (resume/auto-advance), and the in-cast audio-language switch."""

from __future__ import annotations

import pytest

from nstream import caster
from nstream.config import Config

CFG = Config(torrentio_base="tb")


# --- device resolution -----------------------------------------------------


def _scan(devs):
    return lambda: list(devs)  # devs: [(name, ip)]


def test_resolve_device_pref_present_returns_ip(monkeypatch):
    monkeypatch.setattr(
        caster.settings,
        "scan_devices",
        _scan([("Salotto", "192.168.1.5"), ("Camera", "192.168.1.6")]),
    )
    cfg = Config(torrentio_base="tb", cast_device="Salotto")
    assert caster.resolve_device(cfg) == "192.168.1.5"  # preferred name → its current IP


def test_resolve_device_pref_absent_rediscovers(monkeypatch):
    # Pinned name not on this LAN (network changed) → re-discover, don't return it stale.
    monkeypatch.setattr(caster.settings, "scan_devices", _scan([("Camera", "192.168.1.6")]))
    cfg = Config(torrentio_base="tb", cast_device="Salotto")
    assert caster.resolve_device(cfg) == "192.168.1.6"


def test_resolve_device_single_auto_ip(monkeypatch):
    monkeypatch.setattr(caster.settings, "scan_devices", _scan([("TV1", "10.0.0.9")]))
    assert caster.resolve_device(Config(torrentio_base="tb")) == "10.0.0.9"


def test_resolve_device_multiple_prompts_ip(monkeypatch):
    monkeypatch.setattr(
        caster.settings, "scan_devices", _scan([("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")])
    )
    monkeypatch.setattr(caster, "fzf", lambda items, prompt: "10.0.0.2")
    assert caster.resolve_device(Config(torrentio_base="tb")) == "10.0.0.2"


def test_resolve_device_choose_forces_picker_by_name(monkeypatch):
    monkeypatch.setattr(caster.settings, "scan_devices", _scan([("TV1", "10.0.0.1")]))
    seen = {}

    def fk(items, prompt):
        seen["items"] = items
        return "10.0.0.1"

    monkeypatch.setattr(caster, "fzf", fk)
    assert caster.resolve_device(Config(torrentio_base="tb"), choose=True) == "10.0.0.1"
    assert seen["items"] == [("TV1", "10.0.0.1")]  # label=name, value=ip


def test_resolve_device_none_raises(monkeypatch):
    # Empty scan → trust it (no fall-through to a stale cast-resolve default).
    monkeypatch.setattr(caster.settings, "scan_devices", _scan([]))
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(Config(torrentio_base="tb"))


def test_resolve_device_cancel_raises(monkeypatch):
    monkeypatch.setattr(
        caster.settings, "scan_devices", _scan([("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")])
    )
    monkeypatch.setattr(caster, "fzf", lambda items, prompt: None)
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(Config(torrentio_base="tb"))


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
    pos, dur, advance = caster.cast(CFG, "Show E1", "http://u", device="TV", next_label="Show E2")
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
    pos, dur, advance = caster.cast(CFG, "Show E1", "http://u", device="TV", next_label="Show E2")
    assert (pos, dur) == (20.0, 100.0)
    assert advance is False


def test_cast_launch_failure_returns_zero(monkeypatch):
    _cast_run(monkeypatch, launch_rc=1)
    assert caster.cast(CFG, "M", "http://u", device="TV") == (0.0, 0.0, False)


def test_cast_gives_up_if_never_starts(monkeypatch):
    # Receiver stays idle/unreachable forever → bail after _CAST_GIVEUP polls,
    # never loops indefinitely.
    calls = _cast_run(monkeypatch, info_seq=[])  # every info poll fails
    assert caster.cast(CFG, "M", "http://u", device="TV") == (0.0, 0.0, False)
    info_polls = sum(1 for c in calls if "info" in c)
    assert info_polls == caster._CAST_GIVEUP


def test_cast_prints_preparing_before_launch(monkeypatch, capsys):
    _cast_run(monkeypatch, info_seq=[{"player_state": "IDLE"}])
    caster.cast(CFG, "Dune", "http://u", device="TV")
    assert "preparo il cast" in capsys.readouterr().err


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
        caster.settings, "scan_devices", _scan([("TV1", "10.0.0.1"), ("TV2", "10.0.0.2")])
    )
    monkeypatch.setattr(caster, "fzf", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fzf")))
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(CFG, headless=True)


def test_resolve_device_headless_prefers_named(monkeypatch):
    monkeypatch.setattr(
        caster.settings, "scan_devices", _scan([("TV1", "10.0.0.1"), ("Salotto", "10.0.0.2")])
    )
    assert caster.resolve_device(CFG, headless=True, prefer="Salotto") == "10.0.0.2"


def test_resolve_device_prefer_absent_raises(monkeypatch):
    monkeypatch.setattr(caster.settings, "scan_devices", _scan([("TV1", "10.0.0.1")]))
    with pytest.raises(caster.CastUnavailable):
        caster.resolve_device(CFG, prefer="Salotto")
