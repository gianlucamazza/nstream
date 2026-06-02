"""Watch-history persistence for resume and continue-watching.

State lives in ``XDG_STATE_HOME/nstream/history.json`` as a mapping
``video_id -> HistoryEntry``. All reads are best-effort: a missing or corrupt
file yields an empty history so playback is never blocked by state errors.
"""

from __future__ import annotations

import json
import os

from .config import Config, HistoryEntry, state_path

# Past this fraction of the runtime a title counts as watched and drops out of
# the continue-watching list.
WATCHED_THRESHOLD = 0.9


def load_history(cfg: Config) -> dict[str, HistoryEntry]:
    if not cfg.history_enabled:
        return {}
    try:
        data = json.loads(state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _watched(entry: HistoryEntry) -> bool:
    duration = entry.get("duration") or 0.0
    return duration > 0 and (entry.get("position") or 0.0) / duration > WATCHED_THRESHOLD


def save_entry(cfg: Config, entry: HistoryEntry) -> None:
    if not cfg.history_enabled:
        return
    history = load_history(cfg)
    vid = entry.get("video_id")
    if not vid:
        return
    if _watched(entry):
        history.pop(vid, None)
    else:
        history[vid] = entry

    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def recent(cfg: Config, limit: int = 30) -> list[HistoryEntry]:
    entries = [e for e in load_history(cfg).values() if not _watched(e)]
    entries.sort(key=lambda e: e.get("ts", 0.0), reverse=True)
    return entries[:limit]
