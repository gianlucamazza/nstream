"""Shared castbridge delivery driver (ADR 0011).

The bridge event-consumption loop — "failed before `started` → fall back to catt,
after → no fallback", started announce, pos/dur tracking, the finished (advance)
heuristic, KeyboardInterrupt policy — used to live in THREE hand-synced copies:
`caster._cast_via_bridge` and both branches of `remux._cast_file_via_bridge`. They
had already drifted (the finish heuristic existed only in caster, `disconnected`
handling only in remux) and every cross-cutting change cost 3×. `drive_bridge` is
the single body; the callers stay thin adapters supplying their delivery-specific
lifecycle via the small callback hooks (server spawn/teardown, catt fallback
mechanics stay per-delivery — see the ADR's rationale).

Leaf module below `cli` (imports only `bridge`/`log` + stdlib), like `bridge`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from . import bridge, log

_log = log.get_logger("cast_delivery")

# `--follow` JSONL callback: receives each normalized castbridge event.
EventCb = Callable[[dict], None]

# Fraction of the runtime past which an `ended` event counts as finished — the series
# auto-advance heuristic (a manual stop mid-episode must not binge ahead).
CAST_DONE = 0.97


class BridgeOutcome(NamedTuple):
    """What one bridge cast did. `finished` is the advance heuristic (ended at ≥
    CAST_DONE of the runtime); `disconnected` means the daemon socket died mid-cast
    (NOT a playback end — the receiver may still be streaming)."""

    pos: float
    dur: float
    started: bool
    finished: bool
    disconnected: bool


def drive_bridge(
    device: str,
    url: str,
    *,
    follow: bool,
    load_kwargs: dict,
    on_event: EventCb | None = None,
    on_started: Callable[[], None] | None = None,
    on_disconnect: Callable[[float], None] | None = None,
    on_interrupt: Callable[[bool], bool] | None = None,
) -> BridgeOutcome | None:
    """Consume one `bridge.cast_load` event stream and apply the shared policy.

    Returns **None** when the LOAD failed before `started` — the caller falls back to
    catt (a media error the receiver reports *after* started ends the cast without a
    fallback: catt wouldn't fare better). Otherwise returns the `BridgeOutcome`; the
    caller decides what a never-started-but-not-failed stream means for its delivery.

    Hooks (all optional):
    - `on_started()` — announce, fired once on the first `started` event;
    - `on_disconnect(pos)` — when given, a `disconnected` event stops following (the
      hook warns, the loop breaks and the outcome flags it); when None the event only
      updates pos/dur and the stream is left to end on its own;
    - `on_interrupt(started) -> bool` — Ctrl-C policy. The driver always stops the
      receiver session first (`bridge.stop`); the hook does delivery-specific cleanup
      and returns True to re-raise (headless "user abort" semantics) or False to
      swallow and return the outcome (interactive "stop following" semantics —
      the default when no hook is given).
    """
    started = False
    finished = False
    disconnected = False
    pos = dur = 0.0
    events = bridge.cast_load(device, url, follow=follow, **load_kwargs)
    try:
        for ev in events:
            kind = ev.get("kind")
            if kind == "failed" and not started:
                _log.warning(
                    "castbridge non partito (%s: %s) → fallback catt",
                    ev.get("error") or "?",
                    ev.get("message") or "?",
                )
                return None
            if kind == "started" and not started:
                started = True
                if on_started:
                    on_started()
            if on_event:
                on_event(ev)
            if kind in ("playing", "paused", "ended", "disconnected"):
                pos = float(ev.get("position") or pos)
                dur = float(ev.get("duration") or dur)
            if kind == "ended":
                finished = bool(dur) and pos >= dur * CAST_DONE
            if kind == "disconnected" and on_disconnect is not None:
                on_disconnect(pos)
                disconnected = True
                break
    except KeyboardInterrupt:
        bridge.stop(device)
        if on_interrupt is not None and on_interrupt(started):
            raise
    finally:
        events.close()
    return BridgeOutcome(pos, dur, started, finished, disconnected)
