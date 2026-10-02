"""Series-only flow: episode picker, binge auto-advance, per-episode resume.

Sits below `cli` in the import graph and never imports it: the player entry point
(`cli._play_video` with cfg and typ="series" pre-bound) arrives as the injected
`PlayVideo` callable — the same inversion as the `on_save` history hook — so this
module touches no playback machinery directly (ADR 0009)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import NamedTuple, Protocol

from . import api, state, ui
from .caster import CastMeta
from .config import Config, PlayOpts
from .labels import display_title, episode_label
from .picker import fzf, fzf_key
from .types import HistoryEntry, Meta, Video

# Above this many episodes (or when ≥2 seasons exist) the picker is season-first
# so long series stay scannable. Below: flat list as before.
_SEASON_THRESHOLD = 40


class PlayVideo(Protocol):
    """The injected player entry point: `cli._play_video` with cfg and typ="series"
    pre-bound. Returns (notice, advance, quality) — see `_play_video` for the semantics.
    `quality` (0 = Auto, N = exact res) is sticky across binge episodes."""

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
        prefetch: tuple[str, str] | None = None,
    ) -> tuple[str | None, bool, int]: ...


def entry_video(entry: HistoryEntry) -> Video | None:
    """Season/episode of a series history entry (for `display_title`); None for movies."""
    if entry.get("type") != "series":
        return None
    return {"season": entry.get("season", 0), "episode": entry.get("episode", 0)}


class NextUp(NamedTuple):
    """What should play for a history entry. `selection` doubles as the headless JSON
    field: "resume" (same video), "next" (`video` is the following episode), "completed"
    (the series has no episode after this one, `video` is None)."""

    video: Video | None
    selection: str


def next_video(eps: list[Video], current: Video | None) -> Video | None:
    """The episode right after `current` in `eps`, or None past the finale.

    Order is canonical `(season, episode)` and the successor is the first tuple STRICTLY
    greater, so a season boundary is not a special case: S01E13 → S02E01 falls out of the
    same comparison. A `current` with no usable season/episode returns None — an unknown
    position is not "before S01E01", and advancing from it would restart the series."""
    cur = (current or {}).get("season") or 0, (current or {}).get("episode") or 0
    if cur == (0, 0):
        return None
    return next((v for v in eps if (v.get("season") or 0, v.get("episode") or 0) > cur), None)


def next_up(cfg: Config, entry: HistoryEntry, *, eps: list[Video] | None = None) -> NextUp:
    """What to continue from a history entry — the single answer to the question `-c` asks,
    interactive or headless (ADR 0029). Before this, the two paths answered differently: the
    headless one advanced past a finished episode while the TUI simply hid it.

    The order of the cases IS the policy: a film or an unfinished episode resumes; an entry
    that doesn't know where it sits in the series resumes (it cannot advance); an unreadable
    episode list resumes (a catalogue hiccup must not block playback); a finished episode
    advances, or reports the series completed. `eps` is injectable because the binge already
    holds the list — the resume path never pays an `api.episodes` it doesn't need."""
    video = entry_video(entry)
    if video is None:  # a movie has no continuation
        return NextUp(None, "resume")
    if not state.is_watched(entry):
        return NextUp(video, "resume")
    if (video.get("season") or 0, video.get("episode") or 0) == (0, 0):
        return NextUp(video, "resume")  # legacy/unpositioned entry: replay, never restart
    series_id = entry.get("series_id") or entry.get("video_id", "")
    episodes = eps if eps is not None else (api.episodes(cfg, series_id) if series_id else [])
    if not episodes:
        return NextUp(video, "resume")
    nxt = next_video(episodes, video)
    return NextUp(nxt, "next") if nxt is not None else NextUp(None, "completed")


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
        nxt = next_video(eps, video)  # shared policy: season boundaries, gaps, ordering
        next_label = display_title(name, nxt) if (opts.autoplay and nxt is not None) else None

        on_save = state.progress_saver(
            cfg, video_id, name, "series", series_id=series_id, video=video
        )

        notice, advance, quality = play_video(
            video_id, display_title(name, video), opts,
            auto=auto, next_label=next_label, on_save=on_save,
            reselect_on_wrong_audio=not unattended,  # binge advances warn-and-proceed, don't block
            # The next episode, prepared near this one's end (live tier: ~instant change).
            prefetch=(nxt["id"], display_title(name, nxt)) if next_label and nxt else None,
            cast_meta=CastMeta(
                poster=poster, series_title=name,
                season=video.get("season", 0) or 0, episode=video.get("episode", 0) or 0,
            ),
        )  # fmt: skip
        # Sticky quality for subsequent episodes (skip the in-flow picker / keep filter).
        opts = replace(opts, quality=quality)
        if notice:
            return notice
        if not advance or nxt is None:
            return None
        # Follow the policy's pick, not idx+1: the two agree on a well-formed list, and when
        # they don't (gaps, duplicates) the policy is the one that decided `next_label`.
        idx = next((i for i, v in enumerate(eps) if v.get("id") == nxt.get("id")), -1)
        if idx < 0:
            return None
        auto = True
        unattended = True
        ui.status(f"prossimo: {display_title(name, nxt)}", kind="play")
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
    return to the picker when playback ends or backs out. Multi-season / long series
    get a season menu first. `pick_hint`/`apply_key` are cli's leaf-list helpers,
    injected like the player. Returns a notice, or None."""
    name = meta.get("name", "nstream")
    eps = api.episodes(cfg, meta["id"])
    if not eps:
        return f"nessun episodio per «{name}»"
    sid = meta["id"]
    seasons = sorted({int(v.get("season") or 0) for v in eps if (v.get("season") or 0) > 0})
    season_first = len(seasons) >= 2 or len(eps) > _SEASON_THRESHOLD

    def ep_preview(v: Video) -> str:
        return f"episode {sid} {v.get('season', 0)} {v.get('episode', 0)}"

    def pick_and_binge(subset: list[Video]) -> bool:
        """Episode list loop. Returns True when the user ESCs (caller may reopen a
        season menu); never returns a binge notice — those stay as the list header."""
        items = [(episode_label(v), v) for v in subset]
        notice: str | None = None
        while True:
            chosen = fzf_key(
                items, "episodio> ", header=notice or pick_hint(opts), preview=ep_preview
            )
            if not chosen:
                return True
            key, start_video = chosen
            notice = binge(
                cfg, meta["id"], name, eps, start_video, apply_key(opts, key),
                play_video=play_video, poster=meta.get("poster") or "",
            )  # fmt: skip

    if not season_first:
        pick_and_binge(eps)
        return None

    # Season-first: pick a season (or "all"), then the episode list for that subset.
    g = ui.glyphs(ui.active_caps())
    _ALL = object()
    season_items: list[tuple[str, object]] = [(f"{g.series}  Stagione {s:02d}", s) for s in seasons]
    season_items.append((f"{g.series}  Tutte le stagioni", _ALL))
    while True:
        pick = fzf(season_items, "stagione> ", header=pick_hint(opts))
        if pick is None:
            return None
        subset = eps if pick is _ALL else [v for v in eps if int(v.get("season") or 0) == pick]
        if not subset:
            continue
        pick_and_binge(subset)  # ESC → reopen season menu


def resume(
    cfg: Config, entry: HistoryEntry, opts: PlayOpts, *, play_video: PlayVideo
) -> str | None:
    """Resume a series history entry: keep bingeing from the right episode when possible,
    else replay just that one. Returns a notice to show, or None.

    Which episode is "the right one" is `next_up`'s call, not this function's — a finished
    episode continues with the NEXT one, exactly as the headless `-c` has always done
    (ADR 0029); before that parity, the TUI replayed the credits it had just watched."""
    name = entry.get("title", "nstream")
    series_id = entry.get("series_id", "")
    target: Video | None = None  # the next episode, when the entry's one is finished
    if series_id:
        eps = api.episodes(cfg, series_id)
        nu = next_up(cfg, entry, eps=eps)
        if nu.selection == "completed":
            return f"«{name}» è finita: nessun episodio dopo questo"
        target = nu.video if nu.selection == "next" else None
        if opts.autoplay:
            # Autoplay only decides whether to keep chaining episodes — not WHICH one
            # comes next (that is `next_up`'s call on both settings, ADR 0029).
            start = target or next((v for v in eps if v.get("id") == entry["video_id"]), None)
            if start is not None:
                return binge(cfg, series_id, name, eps, start, opts, play_video=play_video)

    video_id = target.get("id", entry["video_id"]) if target else entry["video_id"]
    season = int(target.get("season") or 0) if target else entry.get("season", 0)
    episode = int(target.get("episode") or 0) if target else entry.get("episode", 0)
    shown = target if target else entry_video(entry)

    on_save = state.progress_saver(
        cfg, video_id, name, "series", series_id=series_id, season=season, episode=episode
    )

    notice, _, _ = play_video(
        video_id, display_title(name, shown), opts,
        auto=opts.auto, next_label=None, on_save=on_save,
    )  # fmt: skip
    return notice
