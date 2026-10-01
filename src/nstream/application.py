"""Shared local playback use case. Frontends own navigation and serialization."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from . import log, player, subs
from .config import Config, PlayOpts
from .playback import PlaybackOutcome, require_started
from .types import Stream

_log = log.get_logger("application")


@dataclass(frozen=True)
class LocalRequest:
    title: str
    typ: str
    video_id: str
    stream: Stream
    opts: PlayOpts
    work_dir: str
    start: float | None = None
    next_label: str | None = None
    cast_enabled: bool = False
    auto: bool = True
    safety_sub_lang: str | None = None


@dataclass(frozen=True)
class LocalResult:
    playback: PlaybackOutcome
    subtitles: subs.SubsPick


def play_local(
    cfg: Config,
    request: LocalRequest,
    *,
    backend: Callable[..., PlaybackOutcome] = player.play,
    acquire_subs: Callable[..., subs.SubsPick] = subs.auto_subs,
    choose_tracks: Callable | None = None,
) -> LocalResult | None:
    """Apply identical language/subtitle policy and evidence checks in TUI and JSON.

    None means cancellation of the manual track picker. Backend failures propagate
    as PlaybackError; callers decide how to present them and whether to persist.
    """
    started = time.monotonic()
    aid: int | None = None
    sid: int | str | None = None
    if request.auto or choose_tracks is None:  # no frontend menu → the automatic policy
        pick = acquire_subs(
            cfg,
            request.typ,
            request.video_id,
            request.work_dir,
            request.opts,
            safety_sub_lang=request.safety_sub_lang,
            video_url=request.stream.get("url"),
            filename=subs.stream_filename(request.stream),
        )
        subs.report_safety_subs(pick, request.safety_sub_lang)
        subs.report_unverified(pick, hint="se sfasati: z / Z in mpv")
    else:
        tracks = choose_tracks(
            cfg, request.stream["url"], request.typ, request.video_id, request.work_dir
        )
        if tracks is None:
            return None
        aid, sid, paths = tracks
        pick = subs.SubsPick(paths)
    _log.debug("phase=subtitles elapsed_ms=%.1f", (time.monotonic() - started) * 1000)
    outcome = require_started(
        backend(
            cfg,
            request.title,
            request.stream["url"],
            start=request.start,
            sub_paths=pick.paths,
            audio_id=aid,
            sub_id=sid,
            next_label=request.next_label,
            cast_enabled=request.cast_enabled,
            work_dir=request.work_dir,
            audio_lang=request.opts.audio_lang,
        )
    )
    return LocalResult(outcome, pick)
