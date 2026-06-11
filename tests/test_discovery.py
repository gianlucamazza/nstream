"""Unit tests for background Chromecast discovery: the catt-scan primitive, the on-disk
device cache, the reachability probe and the background-scan state machine."""

from __future__ import annotations

import json
import threading
import time

import pytest

from nstream import discovery


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    """Isolate the disk cache and reset the module-level scan singleton per test."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    discovery._reset()
    yield
    discovery._reset()


# --- scan primitive ---------------------------------------------------------


def test_scan_sync_parses_catt_scan(monkeypatch):
    class _P:
        returncode = 0
        stdout = (
            "Scanning Chromecasts...\n"
            "192.168.1.228 - 43PUS9235/12 - Philips TPM191E\n"
            "192.168.1.50 - Soggiorno - Google Nest\n"
            "192.168.1.228 - 43PUS9235/12 - Philips TPM191E\n"  # dup
        )

    monkeypatch.setattr(discovery.util, "run_cmd", lambda *a, **k: _P())
    assert discovery.scan_sync() == [
        ("43PUS9235/12", "192.168.1.228"),
        ("Soggiorno", "192.168.1.50"),
    ]


def test_scan_sync_empty_on_failure(monkeypatch):
    # run_cmd returns None when catt is missing or the scan fails.
    monkeypatch.setattr(discovery.util, "run_cmd", lambda *a, **k: None)
    assert discovery.scan_sync() == []


def test_scan_sync_retries_on_empty(monkeypatch):
    # A cold mDNS scan can come back empty; the next attempt finds the device.
    class _P:
        returncode = 0
        stdout = "192.168.1.228 - 43PUS9235/12 - Philips TPM191E\n"

    results = [None, _P()]  # first scan empty (None), second populated
    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        return results[len(calls) - 1]

    monkeypatch.setattr(discovery.util, "run_cmd", fake_run)
    assert discovery.scan_sync() == [("43PUS9235/12", "192.168.1.228")]
    assert len(calls) == 2  # retried once


def test_scan_sync_no_retry_when_populated(monkeypatch):
    # A populated scan is trustworthy and used immediately — no wasted second scan.
    class _P:
        returncode = 0
        stdout = "192.168.1.50 - Soggiorno - Google Nest\n"

    calls = []

    def fake_run(*a, **k):
        calls.append(a)
        return _P()

    monkeypatch.setattr(discovery.util, "run_cmd", fake_run)
    assert discovery.scan_sync() == [("Soggiorno", "192.168.1.50")]
    assert len(calls) == 1


# --- disk cache -------------------------------------------------------------


def test_cache_round_trip():
    discovery.save_cache([("TV", "1.2.3.4"), ("Camera", "5.6.7.8")])
    assert discovery.load_cache() == [("TV", "1.2.3.4"), ("Camera", "5.6.7.8")]


def test_cache_missing_returns_empty():
    assert discovery.load_cache() == []


def test_cache_stale_returns_empty(monkeypatch):
    discovery.save_cache([("TV", "1.2.3.4")])
    real = time.time()
    monkeypatch.setattr(discovery.time, "time", lambda: real + discovery.CACHE_TTL + 1)
    assert discovery.load_cache() == []


def test_cache_corrupt_returns_empty():
    path = discovery._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert discovery.load_cache() == []


def test_cache_bad_entries_filtered():
    # Hand-written/garbled entries must not crash callers expecting (name, ip) pairs.
    path = discovery._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), "devices": [["TV", "1.2.3.4"], ["lonely"], "x", [1, 2]]}
    path.write_text(json.dumps(payload))
    assert discovery.load_cache() == [("TV", "1.2.3.4")]


def test_save_cache_empty_keeps_existing():
    # An empty scan is often mDNS flakiness: it must never wipe a good cache.
    discovery.save_cache([("TV", "1.2.3.4")])
    discovery.save_cache([])
    assert discovery.load_cache() == [("TV", "1.2.3.4")]


def test_save_cache_swallows_oserror(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(discovery.util, "atomic_write", boom)
    discovery.save_cache([("TV", "1.2.3.4")])  # must not raise (best-effort I/O)


# --- reachability probe -----------------------------------------------------


def test_verify_reachable(monkeypatch):
    seen = {}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_connect(addr, timeout=None):
        seen["addr"], seen["timeout"] = addr, timeout
        return _Conn()

    monkeypatch.setattr(discovery.socket, "create_connection", fake_connect)
    assert discovery.verify("1.2.3.4") is True
    assert seen["addr"] == ("1.2.3.4", 8009)
    assert seen["timeout"] == discovery.VERIFY_TIMEOUT


def test_verify_down(monkeypatch):
    def fake_connect(addr, timeout=None):
        raise OSError("no route")

    monkeypatch.setattr(discovery.socket, "create_connection", fake_connect)
    assert discovery.verify("1.2.3.4") is False


# --- background scan state machine ------------------------------------------


def test_background_idle_without_start():
    assert discovery.get_devices(wait=0.0) == ([], "idle")


def test_background_idle_without_catt(monkeypatch):
    monkeypatch.setattr(discovery.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(discovery, "scan_sync", lambda **k: pytest.fail("must not scan"))
    discovery.start_background()
    assert discovery.get_devices(wait=0.0) == ([], "idle")


def test_background_pending_then_fresh(monkeypatch):
    gate = threading.Event()

    def fake_scan(**k):
        gate.wait(5)
        return [("TV", "1.2.3.4")]

    monkeypatch.setattr(discovery.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(discovery, "scan_sync", fake_scan)
    discovery.start_background()
    assert discovery.get_devices(wait=0.0) == ([], "pending")
    gate.set()
    assert discovery.get_devices(wait=5.0) == ([("TV", "1.2.3.4")], "fresh")
    # The worker refreshed the disk cache for the next session.
    assert discovery.load_cache() == [("TV", "1.2.3.4")]


def test_background_start_is_idempotent(monkeypatch):
    calls = []

    def fake_scan(**k):
        calls.append(1)
        return []

    monkeypatch.setattr(discovery.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(discovery, "scan_sync", fake_scan)
    discovery.start_background()
    discovery.start_background()
    assert discovery.get_devices(wait=5.0) == ([], "fresh")  # empty scan is still "fresh"
    assert calls == [1]  # one worker thread, not two


def test_background_empty_scan_keeps_cache(monkeypatch):
    discovery.save_cache([("TV", "1.2.3.4")])
    monkeypatch.setattr(discovery.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(discovery, "scan_sync", lambda **k: [])
    discovery.start_background()
    assert discovery.get_devices(wait=5.0) == ([], "fresh")
    assert discovery.load_cache() == [("TV", "1.2.3.4")]
