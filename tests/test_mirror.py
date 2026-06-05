"""Unit tests for the native mirror cast backend (the executor).

Process, Wayland, and PipeWire I/O are mocked: tests never spawn the sender/mpv, talk to
hyprctl/pactl, or touch the receiver. We test the orchestration logic — null-sink and
headless-output lifecycle, window resolution, state tracking, and idempotent teardown.
"""

from __future__ import annotations

import pytest

from nstream import mirror
from nstream.config import Config


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror, "_state_path", lambda: tmp_path / "mirror.json")
    yield


def _cfg(**kw) -> Config:
    return Config(torrentio_base="tb", **kw)


# --- available() ----------------------------------------------------------


def test_available_false_when_binary_missing(monkeypatch):
    monkeypatch.setattr(mirror, "_sender_bin", lambda: "/no/such/cast_sender")
    assert mirror.available() is False


def test_available_false_when_helpers_missing(monkeypatch, tmp_path):
    fake = tmp_path / "cast_sender"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(mirror, "_sender_bin", lambda: str(fake))
    monkeypatch.setattr(mirror.shutil, "which", lambda _: None)
    assert mirror.available() is False


def test_available_true_when_all_present(monkeypatch, tmp_path):
    fake = tmp_path / "cast_sender"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(mirror, "_sender_bin", lambda: str(fake))
    monkeypatch.setattr(mirror.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert mirror.available() is True


# --- null sink ------------------------------------------------------------


def test_load_null_sink_parses_module_id(monkeypatch):
    class _P:
        returncode = 0
        stdout = "536870916\n"

    monkeypatch.setattr(mirror.subprocess, "run", lambda *a, **k: _P())
    assert mirror._load_null_sink() == 536870916


def test_load_null_sink_none_on_failure(monkeypatch):
    class _P:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(mirror.subprocess, "run", lambda *a, **k: _P())
    assert mirror._load_null_sink() is None


# --- hyprctl resolution ---------------------------------------------------


def test_window_addr_matches_pid(monkeypatch):
    clients = [
        {"pid": 111, "address": "0xAAA"},
        {"pid": 222, "address": "0xBBB"},
    ]
    monkeypatch.setattr(mirror, "_hypr_json", lambda *a: clients)
    assert mirror._window_addr(222) == "0xBBB"
    assert mirror._window_addr(999) is None


def test_headless_workspace(monkeypatch):
    mons = [
        {"name": "eDP-1", "activeWorkspace": {"id": 3}},
        {"name": "HEADLESS-2", "activeWorkspace": {"id": 7}},
    ]
    monkeypatch.setattr(mirror, "_hypr_json", lambda *a: mons)
    assert mirror._headless_workspace("HEADLESS-2") == 7
    assert mirror._headless_workspace("DP-9") is None


def test_create_headless_returns_new_name(monkeypatch):
    names = [{"eDP-1", "DP-2"}, {"eDP-1", "DP-2", "HEADLESS-3"}]
    calls = {"n": 0}

    def _hl():
        i = min(calls["n"], len(names) - 1)
        calls["n"] += 1
        return names[i]

    monkeypatch.setattr(mirror, "_headless_names", _hl)
    monkeypatch.setattr(mirror, "_hypr", lambda *a: "")
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    assert mirror._create_headless() == "HEADLESS-3"


# --- state + teardown -----------------------------------------------------


def test_state_roundtrip_and_clear():
    mirror._write_state({"mpv_pid": 1, "sender_pid": 2})
    assert mirror._read_state() == {"mpv_pid": 1, "sender_pid": 2}
    mirror._clear_state()
    assert mirror._read_state() is None


def test_teardown_is_complete_and_idempotent(monkeypatch):
    killed: list[int] = []
    unloaded: list[int] = []
    removed: list[str] = []
    monkeypatch.setattr(mirror, "_kill", lambda pid: killed.append(pid) if pid else None)
    monkeypatch.setattr(mirror, "_unload_module", lambda mid: unloaded.append(mid) if mid else None)
    monkeypatch.setattr(
        mirror, "_hypr", lambda *a: removed.append(a[-1]) if a[:2] == ("output", "remove") else ""
    )
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)

    state = {"sender_pid": 2, "mpv_pid": 1, "headless": "HEADLESS-2", "sink_module": 99}
    mirror._teardown(state)
    assert killed == [2, 1]
    assert unloaded == [99]
    assert removed == ["HEADLESS-2"]
    assert mirror._read_state() is None
    # Second teardown: nothing left to clear, no crash.
    assert mirror.stop() is False


def test_stop_tears_down_persisted_state(monkeypatch):
    monkeypatch.setattr(mirror, "_kill", lambda pid: None)
    monkeypatch.setattr(mirror, "_unload_module", lambda mid: None)
    monkeypatch.setattr(mirror, "_hypr", lambda *a: "")
    monkeypatch.setattr(mirror.time, "sleep", lambda _: None)
    mirror._write_state({"sender_pid": 5, "headless": "HEADLESS-1", "sink_module": 7})
    assert mirror.stop() is True
    assert mirror._read_state() is None


# --- cast_via_mirror guards ----------------------------------------------


def test_cast_via_mirror_degrades_when_unavailable(monkeypatch):
    monkeypatch.setattr(mirror, "available", lambda: False)
    assert mirror.cast_via_mirror(_cfg(), "Film", "http://u", device="1.2.3.4") == (0.0, 0.0, False)
