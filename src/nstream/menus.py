"""Frontend menus over domain data (ADR 0037): the domain decides, these modules prompt.

`choose_tracks` is the pre-play audio/subtitle track menu over the ffprobe tracks and
OpenSubtitles. It lived in `subs`, which made the subtitle domain import the fzf picker and
the TUI labels; frontends pass it to `application.play_local(choose_tracks=...)`.
"""

from __future__ import annotations

import sys
from typing import cast as typecast

from . import subs, tracks, ui
from .config import Config
from .labels import audio_summary, sub_summary, track_label
from .picker import fzf

# Sentinels: fzf returns None for ESC, so "automatic" can't be a None *value*.
_PLAY, _AUDIO, _SUBS, _AUTO, _OPENSUBS = (object() for _ in range(5))


def choose_tracks(
    cfg: Config, url: str, typ: str, video_id: str, work_dir: str
) -> tuple[int | None, str | int | None, tuple[str, ...]] | None:
    """Pre-play menu to pick the audio/subtitle track from those actually in the file
    (probed with ffprobe). Returns (audio_id, sub_id, sub_paths), or None if the user
    backs out (ESC). With ffprobe unavailable, skips silently to mpv's defaults."""
    tr = tracks.probe_tracks(url)
    if tr.empty():
        print("nstream: tracce non sondabili (ffprobe assente?), uso i default", file=sys.stderr)
        return (None, None, ())

    aid: int | None = None
    sid: int | str | None = None
    sub_paths: tuple[str, ...] = ()
    # Sentinels: fzf returns None for ESC, so "automatic" can't be a None *value*.
    while True:
        items: list[tuple[str, object]] = [
            (f"{ui.g().play}  Avvia", _PLAY),
            (f"{ui.g().audio} Audio: {audio_summary(aid, tr)}", _AUDIO),
            (f"{ui.g().subs} Sottotitoli: {sub_summary(sid, sub_paths, tr)}", _SUBS),
        ]
        chosen = fzf(items, "riproduzione> ")
        if chosen is None:
            return None
        if chosen is _PLAY:
            return (aid, sid, sub_paths)
        if chosen is _AUDIO:
            opts: list[tuple[str, object]] = [("automatico (lingua preferita)", _AUTO)]
            opts += [(track_label(a), a.id) for a in tr.audio]
            pick = fzf(opts, "audio> ")
            if pick is _AUTO:
                aid = None
            elif pick is not None:
                aid = typecast(int, pick)
        else:  # _SUBS
            sopts: list[tuple[str, object]] = [("nessuno", "no")]
            sopts += [(track_label(s), s.id) for s in tr.subs]
            sopts.append(("OpenSubtitles… (esterni)", _OPENSUBS))
            pick = fzf(sopts, "sottotitoli> ")
            if pick is None:
                continue
            if pick is _OPENSUBS:
                got = subs.pick_subtitles(
                    cfg, typ, video_id, work_dir, mode="menu", video_url=url, choose=fzf
                )
                if got:
                    sub_paths, sid = got, None
            else:
                sid, sub_paths = typecast("str | int", pick), ()
