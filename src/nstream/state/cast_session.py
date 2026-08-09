"""Fire-and-return cast session (receiver position merge)."""

from __future__ import annotations

import re
import time
from typing import cast

from .. import util
from ..config import Config
from ..types import HistoryEntry
from .history import save_entry

CAST_SESSION = "watch"

# Past this age a session no longer plausibly describes what's on the TV (any film plus
# a generous pause fits well within it; every new cast rewrites the session anyway).
CAST_SESSION_TTL = 6 * 3600.0


def clear_cast_session() -> None:
    """Drop the fire-and-return cast session, if any. Called at the start of every new
    cast (`cast_flow.run_cast` / Alt-C): the new content replaces what the session
    described, and a stale session would attribute the receiver's position to it."""
    util.RunState(CAST_SESSION).clear()


def expire_cast_session() -> None:
    """Best-effort: drop the cast session once its TTL has passed. Called at every
    headless entry, so an agent that never issues `--stop` doesn't leave a dead session
    around for a later poll to trip on."""
    run_state = util.RunState(CAST_SESSION)
    session = run_state.read()
    if session and time.time() - (session.get("ts") or 0.0) > CAST_SESSION_TTL:
        run_state.clear()


def _norm_title(s: str) -> str:
    """Casefold + alnum-only for tolerant title comparison. Deliberate small duplicate
    of `api.norm_text`: state must not import api (layering)."""
    return "".join(c for c in s.casefold() if c.isalnum())


def _session_title_matches(session_title: str, receiver_title: str) -> bool:
    """Whether the receiver's now-playing title plausibly IS the session's content.
    The receiver title varies by sender — castbridge reports the decorated display
    title ("Mr. Robot · S01E04 · …"), catt the release filename, and the Tier-2 catt
    fallback our own `cast-*.mp4` temp name — so match by normalized substring in
    either direction, and treat an empty/artifact title as not-applicable (True:
    the session TTL decides alone)."""
    r = _norm_title(receiver_title)
    if not r or re.fullmatch(r"cast[0-9a-z_]*mp4", r):
        return True
    s = _norm_title(session_title)
    if not s:
        return True
    return s in r or r in s


def cast_session_info() -> dict | None:
    """Live fire-and-return cast session payload (title, device, history fields), or None
    when missing/expired. Pure read for the TUI home row and lifecycle actions — no discovery."""
    session = util.RunState(CAST_SESSION).read()
    if not session:
        return None
    if time.time() - (session.get("ts") or 0.0) > CAST_SESSION_TTL:
        return None
    return session


def cast_session_device() -> str | None:
    """The device of a live fire-and-return cast session, or None when there is none / it
    expired. Lets `-c` ask that exact receiver for the real position before deciding what to
    continue (ADR 0029) — no discovery, no cost when no session exists."""
    session = cast_session_info()
    if not session:
        return None
    return session.get("device") or None


def cast_session_label(session: dict | None = None) -> str | None:
    """One-line TUI label for an active cast session, or None when none. Pure presentation
    (no glyph set — callers prefix with ui.g().tv)."""
    s = session if session is not None else cast_session_info()
    if not s:
        return None
    title = (s.get("title") or "?").strip() or "?"
    device = (s.get("device") or "").strip()
    if device:
        return f"In onda · {title} · {device}"
    return f"In onda · {title}"


def remember_cast(cfg: Config, entry: HistoryEntry, device: str | None) -> None:
    """Persist the fire-and-return cast session (entry + device) across nstream runs.
    Interactive and `--follow` casts don't need this — their poll loop saves directly."""
    if not cfg.history_enabled:
        return
    util.RunState(CAST_SESSION).write({**entry, "device": device or ""})


def update_from_receiver(
    cfg: Config,
    device: str | None,
    position: float,
    duration: float,
    *,
    title: str | None = None,
    clear: bool = False,
) -> bool:
    """Merge a receiver-reported position into the session entry saved by `remember_cast`,
    if one exists for `device` and still plausibly describes what the TV is playing.
    Returns True when the merged position was accepted (written, or the entry retired by
    the watched logic). `clear` drops the session file afterwards (the `--stop` one-shot);
    a zero/idle position still clears but persists nothing. Two staleness guards protect
    the entry from a position that belongs to some other content: the session TTL, and an
    opportunistic match of `title` (the receiver's now-playing title, when it carries one)
    against the session's — a stale session is dropped so later polls can't corrupt it."""
    run_state = util.RunState(CAST_SESSION)
    session = run_state.read()
    if not session:
        return False
    if time.time() - (session.get("ts") or 0.0) > CAST_SESSION_TTL:
        run_state.clear()
        return False
    # No clear on a device mismatch: the session may belong to another (still live) TV.
    if device and session.get("device") and session["device"] != device:
        return False
    if title and not _session_title_matches(session.get("title") or "", title):
        run_state.clear()  # the TV is playing something else: this session is dead
        return False
    if clear:
        run_state.clear()
    if not (position > 0 and duration > 0):
        return False
    merged = {k: v for k, v in session.items() if k != "device"}
    merged.update(position=position, duration=duration, ts=time.time())
    save_entry(cfg, cast(HistoryEntry, merged))
    return True
