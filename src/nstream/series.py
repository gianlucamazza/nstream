"""Series-only flow: episode picker, binge auto-advance, per-episode resume.

Sits below `cli` in the import graph and never imports it: the player entry point
(`cli._play_video` with cfg and typ="series" pre-bound) arrives as the injected
`PlayVideo` callable — the same inversion as the `on_save` history hook — so this
module touches no playback machinery directly (ADR 0009)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Protocol

from . import api, state
from .caster import CastMeta
from .config import Config, HistoryEntry, Meta, PlayOpts, Video
from .labels import display_title, episode_label
from .picker import fzf_key


class PlayVideo(Protocol):
    """The injected player entry point: `cli._play_video` with cfg and typ="series"
    pre-bound. Returns (notice, advance) — see `_play_video` for the semantics."""

    def __call__(
        self,
        video_id: str,
        title: str,
        opts: PlayOpts,
        *,
        auto: bool,
        next_label: str | None,
        on_save: Callable[[float, float], None],
        reselect_on_wrong_audio: bool = True,
        cast_meta: CastMeta | None = None,
    ) -> tuple[str | None, bool]: ...


def entry_video(entry: HistoryEntry) -> Video | None:
    """Season/episode of a series history entry (for `display_title`); None for movies."""
    if entry.get("type") != "series":
        return None
    return {"season": entry.get("season", 0), "episode": entry.get("episode", 0)}


def binge(
    cfg: Config,
    series_id: str,
    name: str,
    eps: list[Video],
    start_video: Video,
    opts: PlayOpts,
    *,
    play_video: PlayVideo,
    poster: str = "",
) -> str | None:
    """Play a series from `start_video`, auto-advancing through the overlay.
    Returns a notice (e.g. an episode with no streams) to surface, or None."""
    idx = next((i for i, v in enumerate(eps) if v.get("id") == start_video.get("id")), None)
    if idx is None:
        return None
    auto = opts.auto  # the first episode honours --play; binge episodes auto-pick
    unattended = False  # True once we're auto-advancing unattended (no blocking reselection)
    while 0 <= idx < len(eps):
        video = eps[idx]
        video_id = video["id"]
        nxt = eps[idx + 1] if idx + 1 < len(eps) else None
        next_label = display_title(name, nxt) if (opts.autoplay and nxt is not None) else None

        def on_save(pos: float, dur: float, vid: str = video_id, v: Video = video) -> None:
            state.save_entry(
                cfg, state.make_entry(vid, name, "series", pos, dur, series_id=series_id, video=v)
            )

        notice, advance = play_video(
            video_id, display_title(name, video), opts,
            auto=auto, next_label=next_label, on_save=on_save,
            reselect_on_wrong_audio=not unattended,  # binge advances warn-and-proceed, don't block
            cast_meta=CastMeta(
                poster=poster, series_title=name,
                season=video.get("season", 0) or 0, episode=video.get("episode", 0) or 0,
            ),
        )  # fmt: skip
        if notice:
            return notice
        if not advance or nxt is None:
            return None
        idx += 1
        auto = True
        unattended = True
        print(f"▶ Carico {display_title(name, eps[idx])}…", file=sys.stderr)
    return None


def play(
    cfg: Config,
    meta: Meta,
    opts: PlayOpts,
    *,
    play_video: PlayVideo,
    pick_hint: Callable[[PlayOpts], str],
    apply_key: Callable[[PlayOpts, str], PlayOpts],
) -> str | None:
    """Episode picker for a series title: pick an episode (fzf), binge from it, and
    return to the picker when playback ends or backs out. `pick_hint`/`apply_key` are
    cli's leaf-list helpers, injected like the player. Returns a notice, or None."""
    name = meta.get("name", "nstream")
    eps = api.episodes(cfg, meta["id"])
    if not eps:
        return f"nessun episodio per «{name}»"
    items = [(episode_label(v), v) for v in eps]
    sid = meta["id"]

    def ep_preview(v: Video) -> str:
        return f"episode {sid} {v.get('season', 0)} {v.get('episode', 0)}"

    # Loop the episode picker so finishing/backing out returns here, not to the list.
    header: str | None = None
    while True:
        chosen = fzf_key(items, "episodio> ", header=header or pick_hint(opts), preview=ep_preview)
        if not chosen:
            return None
        key, start_video = chosen
        header = binge(
            cfg, meta["id"], name, eps, start_video, apply_key(opts, key),
            play_video=play_video, poster=meta.get("poster") or "",
        )  # fmt: skip


def resume(
    cfg: Config, entry: HistoryEntry, opts: PlayOpts, *, play_video: PlayVideo
) -> str | None:
    """Resume a series history entry: keep bingeing the rest of the season when
    possible, else replay just that episode. Returns a notice to show, or None."""
    name = entry.get("title", "nstream")
    series_id = entry.get("series_id", "")
    if series_id and opts.autoplay:
        eps = api.episodes(cfg, series_id)
        cur = next((v for v in eps if v.get("id") == entry["video_id"]), None)
        if cur is not None:
            return binge(cfg, series_id, name, eps, cur, opts, play_video=play_video)

    video_id = entry["video_id"]

    def on_save(pos: float, dur: float) -> None:
        state.save_entry(
            cfg,
            state.make_entry(
                video_id,
                name,
                "series",
                pos,
                dur,
                series_id=series_id,
                season=entry.get("season", 0),
                episode=entry.get("episode", 0),
            ),  # fmt: skip
        )

    notice, _ = play_video(
        video_id, display_title(name, entry_video(entry)), opts,
        auto=opts.auto, next_label=None, on_save=on_save,
    )  # fmt: skip
    return notice
