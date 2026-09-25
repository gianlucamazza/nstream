"""Per-addon circuit breaker (ADR 0027).

Persisted under a bounded lock in XDG_STATE; contention skips a best-effort update.
Failures that trip the breaker are retryable network failures / gather
timeouts — not empty catalogs (an up addon may legitimately return zero streams).
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from .. import log, util

_log = log.get_logger("breaker")

# Consecutive retryable failures before Open. Matches the field arithmetic of a full
# timeout budget (~80s) being paid a few times without recovery.
FAIL_THRESHOLD = 3
# Seconds to stay Open before a single Half-Open probe is allowed.
OPEN_COOLDOWN_S = 300.0
HALF_OPEN_LEASE_S = 30.0
MAX_ENTRIES = 64

_STATE_NAME = "addon-breakers"


def _path() -> Path:
    base = Path(
        __import__("os").environ.get("XDG_STATE_HOME")
        or __import__("os").path.expanduser("~/.local/state")
    )
    return base / "nstream" / "addon-breakers.json"


def _read() -> dict[str, dict]:
    data = util.load_json(_path(), {})
    if not isinstance(data, dict):
        return {}
    entries = data.get("breakers")
    if not isinstance(entries, dict):
        return {}
    return {
        key: rec
        for key, rec in entries.items()
        if isinstance(rec, dict)
        and all(
            isinstance(rec.get(field, 0), int | float) for field in ("ts", "opened_at", "fails")
        )
    }


def _write(entries: dict[str, dict]) -> None:
    with contextlib.suppress(OSError):
        _path().parent.mkdir(parents=True, exist_ok=True)
        if len(entries) > MAX_ENTRIES:
            keep = sorted(entries.items(), key=lambda kv: kv[1].get("ts", 0.0), reverse=True)
            entries = dict(keep[:MAX_ENTRIES])
        util.atomic_write(
            _path(),
            lambda f: json.dump({"version": 1, "breakers": entries}, f, ensure_ascii=False),
            prefix=".breakers-",
        )


def _now() -> float:
    return time.time()


@util.state_update(_path, True)
def allow(key: str, *, now: float | None = None) -> bool:
    """True if this addon base may be queried. Open → False until cooldown; then Half-Open."""
    if not key:
        return True
    now = _now() if now is None else now
    rec = _read().get(key)
    if not rec:
        return True
    state = rec.get("state") or "closed"
    if state == "closed":
        return True
    if state == "open":
        opened = float(rec.get("opened_at") or 0.0)
        if now - opened >= OPEN_COOLDOWN_S:
            # Transition to half-open in memory for this process; first success/fail settles.
            rec = {**rec, "state": "half_open", "ts": now}
            entries = _read()
            entries[key] = rec
            _write(entries)
            _log.info("breaker half-open: %s", key)
            return True
        return False
    if now - float(rec.get("ts") or 0) < HALF_OPEN_LEASE_S:
        return False
    # A process may have exited while holding the probe lease; allow recovery.
    entries = _read()
    entries[key] = {**rec, "ts": now}
    _write(entries)
    return True


@util.state_update(_path)
def record_success(key: str) -> None:
    if not key:
        return
    entries = _read()
    prev = entries.get(key) or {}
    if prev.get("state") in ("open", "half_open") or prev.get("fails", 0):
        _log.info("breaker closed: %s", key)
    entries[key] = {"state": "closed", "fails": 0, "ts": _now()}
    _write(entries)


@util.state_update(_path)
def record_failure(key: str, *, reason: str = "") -> None:
    """Count a retryable failure. Opens the breaker at FAIL_THRESHOLD."""
    if not key:
        return
    entries = _read()
    rec = dict(entries.get(key) or {})
    state = rec.get("state") or "closed"
    if state == "half_open":
        entries[key] = {
            "state": "open",
            "fails": FAIL_THRESHOLD,
            "opened_at": _now(),
            "ts": _now(),
            "reason": reason or rec.get("reason") or "timeout",
        }
        _write(entries)
        _log.warning("breaker open (half-open failed): %s", key)
        return
    fails = int(rec.get("fails") or 0) + 1
    if fails >= FAIL_THRESHOLD:
        entries[key] = {
            "state": "open",
            "fails": fails,
            "opened_at": _now(),
            "ts": _now(),
            "reason": reason or "network",
        }
        _write(entries)
        _log.warning("breaker open after %d fails: %s", fails, key)
        return
    entries[key] = {
        "state": "closed",
        "fails": fails,
        "ts": _now(),
        "reason": reason or rec.get("reason") or "",
    }
    _write(entries)


def open_breakers(*, now: float | None = None) -> list[dict]:
    """List currently Open (or cooling) breakers for --explain / diagnostics."""
    now = _now() if now is None else now
    out: list[dict] = []
    for key, rec in _read().items():
        if not isinstance(rec, dict):
            continue
        st = rec.get("state") or "closed"
        if st not in ("open", "half_open"):
            continue
        opened = float(rec.get("opened_at") or rec.get("ts") or 0.0)
        out.append(
            {
                "key": key,
                "state": st,
                "opened_at": opened,
                "age_s": max(0.0, now - opened),
                "reason": rec.get("reason") or "",
            }
        )
    out.sort(key=lambda r: r["age_s"], reverse=True)
    return out


@util.state_update(_path, 0)
def forget_breakers() -> int:
    """Clear all breakers (CLI escape hatch, analogue of --forget-dead)."""
    n = len(_read())
    _write({})
    return n
