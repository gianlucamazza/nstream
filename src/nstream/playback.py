"""Playback evidence shared by frontends and delivery adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class PlaybackError(Exception):
    """A backend failed to start or failed while playing. Safe for user output."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PlaybackOutcome:
    position: float = 0.0
    duration: float = 0.0
    signal: str = ""
    started: bool = False
    reason: Literal["ended", "stopped", "cancelled", "failed"] = "stopped"
    error: str | None = None

    def __iter__(self):
        # Preserve the historical in-process unpacking interface during migration.
        yield self.position
        yield self.duration
        yield self.signal


def require_started(outcome: PlaybackOutcome) -> PlaybackOutcome:
    """Never infer successful delivery from a process exit or zero position."""
    if outcome.error or not outcome.started:
        raise PlaybackError(outcome.error or "player_failed", "mpv non ha riprodotto il media")
    return outcome
