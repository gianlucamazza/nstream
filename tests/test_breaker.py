"""Unit tests for per-addon circuit breaker (ADR 0027)."""

from __future__ import annotations

from nstream.state import breaker as brk


def test_closed_allows_and_opens_after_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    key = "https://torrentio.example"
    assert brk.allow(key) is True
    for _ in range(brk.FAIL_THRESHOLD - 1):
        brk.record_failure(key, reason="network")
        assert brk.allow(key) is True
    brk.record_failure(key, reason="timeout")
    assert brk.allow(key) is False
    open_rows = brk.open_breakers()
    assert any(r["key"] == key and r["state"] == "open" for r in open_rows)


def test_success_resets(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    key = "https://comet.example"
    brk.record_failure(key)
    brk.record_failure(key)
    brk.record_success(key)
    assert brk.allow(key) is True
    assert brk._read().get(key, {}).get("fails") == 0


def test_half_open_after_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    key = "https://slow.example"
    now = 1_000_000.0
    monkeypatch.setattr(brk, "_now", lambda: now)
    for _ in range(brk.FAIL_THRESHOLD):
        brk.record_failure(key, reason="timeout")
    assert brk.allow(key, now=now) is False
    # Just before cooldown ends: still Open
    assert brk.allow(key, now=now + brk.OPEN_COOLDOWN_S - 1) is False
    # After cooldown: Half-Open probe allowed
    assert brk.allow(key, now=now + brk.OPEN_COOLDOWN_S + 1) is True
    assert brk._read()[key]["state"] == "half_open"
    # Half-open failure → Open again (opened_at resets to current _now)
    brk.record_failure(key, reason="timeout")
    assert brk._read()[key]["state"] == "open"
    assert brk.allow(key, now=now) is False
    # Still cooling at original + cooldown (opened_at was reset to `now`)
    assert brk.allow(key, now=now + brk.OPEN_COOLDOWN_S - 1) is False


def test_half_open_success_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    key = "https://recover.example"
    now = 2_000_000.0
    monkeypatch.setattr(brk, "_now", lambda: now)
    for _ in range(brk.FAIL_THRESHOLD):
        brk.record_failure(key)
    brk.allow(key, now=now + brk.OPEN_COOLDOWN_S + 1)
    brk.record_success(key)
    assert brk.allow(key) is True
    assert brk._read()[key]["state"] == "closed"


def test_forget_breakers(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    brk.record_failure("a")
    brk.record_failure("b")
    n = brk.forget_breakers()
    assert n == 2
    assert brk._read() == {}
    assert brk.open_breakers() == []


def test_empty_key_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert brk.allow("") is True
    brk.record_failure("")
    brk.record_success("")
    assert brk._read() == {}


def test_state_package_exports(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from nstream import state

    state.breaker_record_failure("http://x")
    assert state.breaker_allow("http://x") is True
    state.breaker_record_success("http://x")
    assert state.forget_breakers() >= 0


def test_cooldown_doubles_per_trip_and_resets_on_success(tmp_path, monkeypatch):
    # A chronically dead addon must not be re-probed every 5 minutes forever.
    from nstream.state import breaker as brk

    now = [1000.0]
    monkeypatch.setattr(brk, "_now", lambda: now[0])
    for _ in range(brk.FAIL_THRESHOLD):
        brk.record_failure("http://dead")
    assert brk.admit("http://dead") is None
    now[0] += brk.OPEN_COOLDOWN_S
    assert brk.admit("http://dead") == "probe"
    brk.record_failure("http://dead")  # half-open probe failed → second trip
    now[0] += brk.OPEN_COOLDOWN_S
    assert brk.admit("http://dead") is None  # cooldown is now 2x
    now[0] += brk.OPEN_COOLDOWN_S
    assert brk.admit("http://dead") == "probe"
    brk.record_success("http://dead")
    assert brk.admit("http://dead") == "normal"


def test_record_success_skips_rewrite_when_already_closed(monkeypatch):
    from nstream.state import breaker as brk

    brk.record_success("http://ok")
    writes = []
    monkeypatch.setattr(brk, "_write", lambda e: writes.append(e))
    brk.record_success("http://ok")
    assert writes == []
