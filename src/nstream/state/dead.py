"""Dead-source denylist persistence (ADR 0025)."""

from __future__ import annotations

import contextlib
import json
import time

from .. import util
from ..config import dead_sources_path

DEAD_TTL = 30 * 86400.0
MAX_DEAD_SOURCES = 500


def _dead_read() -> dict[str, dict]:
    data = util.load_json(dead_sources_path(), {})
    if not isinstance(data, dict):
        return {}
    entries = data.get("sources")
    return entries if isinstance(entries, dict) else {}


def dead_sources(now: float | None = None) -> dict[str, dict]:
    """The non-expired denylist, `key -> {ts, reason}`. Best-effort: a missing or corrupt
    file yields an empty mapping, so a state error can never block playback."""
    now = time.time() if now is None else now
    live: dict[str, dict] = {}
    for key, rec in _dead_read().items():
        if not isinstance(rec, dict):
            continue
        ts = rec.get("ts")
        if isinstance(ts, int | float) and now - ts < DEAD_TTL:
            live[key] = rec
    return live


def is_dead(key: str) -> bool:
    return bool(key) and key in dead_sources()


@util.state_update(dead_sources_path)
def mark_dead(key: str, reason: str = "") -> None:
    """Remember that `key` (infoHash / filename / release name) is provably gone. Never
    called for a transient failure — only for `net.Probe.dead` (ADR 0025)."""
    if not key:
        return
    entries = dead_sources()
    entries[key] = {"ts": time.time(), "reason": reason}
    if len(entries) > MAX_DEAD_SOURCES:  # prune oldest first
        keep = sorted(entries.items(), key=lambda kv: kv[1].get("ts", 0.0), reverse=True)
        entries = dict(keep[:MAX_DEAD_SOURCES])
    _dead_write(entries)


@util.state_update(dead_sources_path, 0)
def forget_dead() -> int:
    """Clear the denylist (user escape hatch). Returns how many entries were dropped."""
    count = len(_dead_read())
    _dead_write({})
    return count


def _dead_write(entries: dict[str, dict]) -> None:
    with contextlib.suppress(OSError):  # state is diagnostics: never fail playback over it
        dead_sources_path().parent.mkdir(parents=True, exist_ok=True)
        util.atomic_write(
            dead_sources_path(),
            lambda f: json.dump({"version": 1, "sources": entries}, f, ensure_ascii=False),
            prefix=".dead-sources-",
        )
