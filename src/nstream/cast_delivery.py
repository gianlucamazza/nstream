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


def is_finished(pos: float, dur: float) -> bool:
    """Whether a cast that ended at `pos` of `dur` reached its natural end (ADR 0029).

    The single definition in the repo: every delivery backend produces `pos`/`dur`, and
    `cast_flow` alone turns them into an advance decision. An unknown duration (`dur == 0`
    — a fire-and-return cast nobody polled) is never a finish: a position we did not
    observe cannot prove the episode ended."""
    return bool(dur) and pos >= dur * CAST_DONE


class CastResult(NamedTuple):
    """What one delivery backend (`caster`, `remux`, `mirror`) observed (ADR 0031).

    `started` is the field that exists: "the cast never began" and "fire-and-return handed
    off fine" both produce `pos == dur == 0.0`, so without it no caller can tell a failure
    from a success, and `headless_play` reported a dead cast as `ok: true`. On fire-and-return
    `started` means the handoff was ACCEPTED (catt rc 0), not that playback was observed —
    the strongest evidence available without a poll loop.

    **Read this by attribute, never by unpacking.** A NamedTuple's defaults govern
    construction arity, not unpacking arity: `pos, dur = CastResult(...)` raises ValueError,
    and splatting one into a wider tuple silently destroys both the type and the arity. A new
    delivery backend returns a CastResult and sets `started`; positional consumption of one
    is a review defect."""

    pos: float
    dur: float
    subs_delivered: bool = False
    started: bool = False
    error: str | None = None  # "catt_missing" | "cast_timeout" | "cast_failed" | …


class BridgeOutcome(NamedTuple):
    """What one bridge cast did. `finished` is the advance heuristic (ended at ≥
    CAST_DONE of the runtime); `disconnected` means the daemon socket died mid-cast
    (NOT a playback end — the receiver may still be streaming)."""

    pos: float
    dur: float
    started: bool
    finished: bool
    disconnected: bool
    error: str | None = None  # "receiver_error" when the TV refused the media pre-start


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

    Returns **None** when the LOAD failed before `started` for a transport reason — the
    caller falls back to catt. A `receiver_error` (the TV refused the media) returns a
    not-started outcome with `error` set instead: catt would load the same media and its
    exit code would read as a start. A media error *after* started ends the cast without a
    fallback either. Otherwise returns the `BridgeOutcome`; the
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
                err = ev.get("error") or "?"
                if err == "receiver_error":
                    # The TV reached and refused the media: catt would hand it the same
                    # url and report a false start. Only transport failures fall back.
                    _log.warning("ricevitore ha rifiutato il media: %s", ev.get("message") or "?")
                    return BridgeOutcome(pos, dur, False, False, False, error=err)
                _log.warning(
                    "castbridge non partito (%s: %s) → fallback catt",
                    err,
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
                finished = is_finished(pos, dur)
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
