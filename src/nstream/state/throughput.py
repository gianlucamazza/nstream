"""Measured source throughput for the live Tier-2 (ADR 0039).

A live cast stalls when the source bitrate exceeds what the debrid delivers. The producer's
start is a free measurement (bytes made per wall second while it runs unpaced); the latest
one per host is kept so the next ranking can prefer a release that fits the link. Best
effort like every store: a failed read is "unknown" (0), a failed write is skipped.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from .. import util

MAX_ENTRIES = 16
# A measurement older than this says little about tonight's link.
MAX_AGE_S = 14 * 86400.0


def _path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"))
    return base / "nstream" / "throughput.json"


def _read() -> dict[str, dict]:
    data = util.load_json(_path(), {})
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, dict):
        return {}
    return {
        k: v
        for k, v in hosts.items()
        if isinstance(v, dict) and isinstance(v.get("bps"), int | float)
        and isinstance(v.get("ts"), int | float)
    }  # fmt: skip


def record(host: str, bytes_per_s: float) -> None:
    """Keep `bytes_per_s` as the latest throughput measured from `host`."""
    if not host or bytes_per_s <= 0:
        return
    hosts = _read()
    hosts[host] = {"bps": float(bytes_per_s), "ts": time.time()}
    if len(hosts) > MAX_ENTRIES:
        hosts = dict(sorted(hosts.items(), key=lambda kv: kv[1]["ts"])[-MAX_ENTRIES:])
    with contextlib.suppress(OSError):
        _path().parent.mkdir(parents=True, exist_ok=True)
        util.atomic_write(
            _path(),
            lambda f: json.dump({"version": 1, "hosts": hosts}, f),
            prefix=".throughput-",
        )


def latest(now: float | None = None) -> float:
    """The most recent throughput (bytes/s) measured from any host, or 0 when unknown."""
    now = time.time() if now is None else now
    fresh = [v for v in _read().values() if now - v["ts"] <= MAX_AGE_S]
    return max(fresh, key=lambda v: v["ts"])["bps"] if fresh else 0.0
