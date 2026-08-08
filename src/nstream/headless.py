"""Headless `--json` subsystem: non-interactive play/cast for agents (no fzf, no TTY).

`run()` is the single seam `cli._dispatch` calls when `--json` is set: it wraps `run_auto`
(title/episode resolution → probe / stop / status / resume → `headless_play.auto_play`) and turns a
NetworkError into a JSON error object, so an agent parsing stdout never sees a human
stderr message in its place. Exactly one JSON object is emitted per invocation (plus one
JSONL line per castbridge event with `--follow`); diagnostics stay on stderr, and no
stream/debrid url or token ever reaches stdout.

Same tier as `stream_select`/`cast_flow`: sits below `cli` (never imports it) and reuses
the same primitives as the interactive flow — `api.streams` → `prepare_stream` →
`auto_subs` → `play` / `cast_flow.run_cast` — but never opens fzf (auto=True,
reselect_on_wrong_audio=False) and never silently falls back to local playback when a
requested cast device is missing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from . import (
    api,
    bridge,
    caster,
    explain,
    headless_play,
    mirror,
    remux,
    series,
    state,
    stream_select,
    util,
)
from .api import CAT_MAP
from .caster import CastUnavailable
from .caster import resolve_device as _resolve_device
from .config import Config, PlayOpts
from .labels import display_title
from .subs import available_subtitle_langs
from .types import HistoryEntry, Meta


def typ_filter(args: argparse.Namespace) -> str | None:
    """Explicit content-type filter from --movies/--series, or None (mixed)."""
    if args.movies:
        return "movie"
    if args.series:
        return "series"
    return None


def _emit_json(obj: dict) -> None:
    """Write one machine-readable JSON object to stdout (progress stays on stderr).
    Never carries a stream/debrid url or token — only descriptive metadata."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class YearMismatch(Exception):
    """Every candidate provably carries a release year other than the one requested
    with an explicit --year (ADR 0030). `years` lists what was on offer."""

    def __init__(self, years: tuple[str, ...]) -> None:
        super().__init__(", ".join(years))
        self.years = years


def _release_span(meta: Meta) -> tuple[int, int] | None:
    """(first, last) release years of a meta, or None when `releaseInfo` is absent or
    unreadable. Cinemeta emits "2006", the closed range "2006-2010", the open-ended
    "2006-" for a running series, and an en-dash variant of both."""
    info = str(meta.get("releaseInfo", "")).strip()
    if not info:
        return None
    parts = [p.strip() for p in re.split(r"[-–—]", info)]
    nums = [int(p) for p in parts if len(p) == 4 and p.isdigit()]
    if not nums:
        return None
    open_ended = len(parts) > 1 and not parts[-1].isdigit()
    return (nums[0], 9999 if open_ended else max(nums))


def _year_matches(meta: Meta, year: str) -> bool | None:
    """Tri-state (ADR 0030): True when the meta's release span contains `year`, False
    when it provably does not, None when the span is unknown. None never refuses — a
    missing `releaseInfo` is absence of evidence, not evidence of the wrong year."""
    span = _release_span(meta)
    if span is None or not year.isdigit():
        return None
    return span[0] <= int(year) <= span[1]


def _select_meta(
    metas: list[Meta], query: str, year: str | None, *, want_series: bool = False
) -> tuple[Meta, str]:
    """Pick a title without fzf. The year filters the WHOLE candidate set first, then an
    exact normalized-name match wins inside that pool. The returned tier says which
    evidence decided: "exact" (the name matched), "year" (only the year disambiguated —
    the localized-title case, where the catalog name is English) or "first" (a guess).

    An explicit --year is a hard constraint: when every candidate provably has another
    year, this raises YearMismatch instead of playing the wrong film (ADR 0030). A year
    merely inferred from a trailing token in the query stays soft — it may reorder, never
    refuse, or "blade runner 2049" and "1917" become unplayable.

    `want_series` (an explicit --season/--episode) prefers series results — a same-named
    movie must not shadow the series the caller is clearly asking an episode of."""
    if want_series:
        series = [m for m in metas if m.get("type") == "series"]
        metas = series or metas
    explicit = year is not None
    q = whole = api.norm_text(query)
    head, _, tail = q.rpartition(" ")
    if head and len(tail) == 4 and tail.isdigit():
        q, year = head, year or tail
    pool, hits = metas, []
    if year:
        verdicts = [(m, _year_matches(m, year)) for m in metas]
        hits = [m for m, v in verdicts if v is True]
        unknown = [m for m, v in verdicts if v is None]
        if explicit and not hits and not unknown:
            offered = dict.fromkeys(str(m.get("releaseInfo", "")) for m in metas)
            raise YearMismatch(tuple(offered))
        pool = hits or unknown or metas
    # Match on the split query AND on the whole one: the trailing token is only *probably*
    # a year, and "Blade Runner 2049" is a name that ends in one.
    exact = [m for m in pool if api.norm_text(m.get("name", "")) in (q, whole)]
    if exact:
        return exact[0], "exact"
    if hits:
        return hits[0], "year"
    return pool[0], "first"


def run(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    """The `--json` entry `cli._dispatch` calls. Catch NetworkError here so a network
    failure emits a JSON error object, not the human stderr message main() would print
    (which would break the agent parsing stdout)."""
    try:
        return run_auto(cfg, args, opts)
    except api.NetworkError as e:
        _emit_json({"ok": False, "error": "network", "message": str(e)})
        return 1


def run_auto(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    """`--json`: headless, non-interactive play/cast. No fzf, no TTY. Emits one JSON
    object on stdout (diagnostics on stderr). Returns 0 on success, 1 on a recoverable
    failure (no result / no stream / device), 2 on a usage error."""
    if args.sub_menu:
        _emit_json(
            {"ok": False, "error": "usage", "message": "--sub-menu incompatibile con --json"}
        )
        return 2
    tfilter = typ_filter(args)
    if tfilter == "movie" and (args.season is not None or args.episode is not None):
        _emit_json(
            {
                "ok": False,
                "error": "usage",
                "message": "--movies incompatibile con --season/--episode",
            }
        )
        return 2
    # Opportunistic hygiene at every headless entry: a cast session whose TTL passed dies
    # even if --stop never came, and Tier-2 leftovers are collected not only when the
    # next remux happens to run.
    state.expire_cast_session()
    remux.gc_stale()
    # Cast lifecycle actions: no title needed, no playback.
    if args.stop:
        return _run_stop(cfg, args)
    if args.status:
        return _run_status(cfg, args)
    if args.pause or args.resume or args.seek is not None:
        return _run_control(cfg, args)
    query = " ".join(args.query)
    if args.volume is not None and not query and not args.browse and not args.cont:
        # Standalone volume: act on the current cast instead of demanding a re-cast.
        return _run_volume(cfg, args)
    if args.cont:
        return _run_auto_resume(cfg, args, opts, query, tfilter)

    # Meta source: a catalog (--browse) or a title search.
    if args.browse:
        cat = CAT_MAP[args.browse]
        metas = api.catalog(cfg, tfilter, cat) if tfilter else api.browse(cfg, cat)
        if not metas:
            _emit_json({"ok": False, "error": "no_result", "message": "catalogo vuoto"})
            return 1
        meta, selection = metas[0], "browse"
    else:
        if not query:
            _emit_json({"ok": False, "error": "usage", "message": "--json richiede un titolo"})
            return 2
        metas = api.search(cfg, query)
        # Explicit --movies/--series: drop the other type before the title match, so a
        # same-named movie can never shadow the series the caller asked for (and vice versa).
        if tfilter:
            metas = [m for m in metas if m.get("type", "movie") == tfilter]
        if not metas:
            what = {"movie": "nessun film", "series": "nessuna serie"}.get(
                tfilter or "", "nessun risultato"
            )
            _emit_json({"ok": False, "error": "no_result", "message": f"{what} per «{query}»"})
            return 1
        # The --season/--episode series inference stays only when no explicit flag is given.
        try:
            meta, selection = _select_meta(
                metas, query, args.year,
                want_series=tfilter is None
                and (args.season is not None or args.episode is not None),
            )  # fmt: skip
        except YearMismatch as e:
            # ADR 0030: an explicit --year contradicted by every candidate refuses rather
            # than guessing — a wrong film cast to the TV is worse than no result.
            _emit_json({
                "ok": False, "error": "no_result",
                "message": f"nessun risultato del {args.year} per «{query}»",
                "years": list(e.years),
            })  # fmt: skip
            return 1
    typ = meta.get("type", "movie")
    name = meta.get("name", "?")
    imdb_id = meta.get("id", "")
    season: int | None = None
    episode: int | None = None
    video_id = imdb_id
    title = display_title(name, None)

    if typ == "series":
        eps = api.episodes(cfg, imdb_id)
        if args.probe and args.episode is None:
            # Episode discovery: `--probe` on a series without --episode lists what
            # exists (callers used to provoke an episode_not_found just to read
            # `available`). --season narrows the list.
            avail = [e for e in eps if not args.season or e.get("season") == args.season]
            _emit_json(
                {
                    "ok": True,
                    "action": "episodes",
                    "title": name,
                    "type": typ,
                    "imdb_id": imdb_id,
                    "episodes": [
                        {
                            "season": e.get("season", 0),
                            "episode": e.get("episode", 0),
                            "title": e.get("name") or "",
                        }
                        for e in avail
                    ][:500],
                    "error": None,
                }
            )
            return 0
        season = args.season or 1
        episode = args.episode or 1
        v = next(
            (e for e in eps if e.get("season") == season and e.get("episode") == episode), None
        )
        if v is None:
            avail = sorted({(e.get("season", 0), e.get("episode", 0)) for e in eps})
            _emit_json(
                {
                    "ok": False,
                    "error": "episode_not_found",
                    "message": f"S{season:02d}E{episode:02d} non trovato per «{name}»",
                    "available": [{"season": s, "episode": ep} for s, ep in avail[:50]],
                }
            )
            return 1
        video_id = v["id"]
        title = display_title(name, v)

    if args.explain:
        # Read-only diagnosis, never plays: the machine-readable twin of the interactive
        # --explain (fzf-driven, unreachable headless — worse, `--json --explain` used to
        # fall through to the PLAY path and cast the title it was asked to explain).
        results = api.streams(cfg, typ, video_id)
        if not results:
            err = stream_select.no_stream_source_error(cfg) or "no_streams"
            _emit_json(
                {
                    "ok": False,
                    "error": err,
                    "message": stream_select.no_streams_message(cfg, typ, video_id, title),
                }
            )
            return 1
        exact = stream_select.exact_resolution(opts.quality)
        _emit_json(
            {
                "ok": True,
                "action": "explain",
                "title": title,
                "type": typ,
                "imdb_id": imdb_id,
                "season": season,
                "episode": episode,
                **explain.explain_data(
                    cfg, results, cast=opts.cast, title=title, exact_resolution=exact
                ),
                "error": None,
            }
        )
        return 0

    if args.probe:
        # Discovery only: list available audio/subtitle languages + resolutions, never play.
        results = api.streams(cfg, typ, video_id)
        _emit_json(
            {
                "ok": True,
                "action": "probe",
                "title": title,
                "type": typ,
                "imdb_id": imdb_id,
                "season": season,
                "episode": episode,
                "available_audio": list(
                    stream_select.audio_languages(cfg, results, cast=opts.cast)
                ),
                "available_subtitles": available_subtitle_langs(cfg, typ, video_id),
                "available_resolutions": stream_select.available_resolutions(
                    cfg, results, cast=opts.cast
                ),
                "error": None,
            }
        )
        return 0

    # Now-playing metadata for the castbridge LOAD (TV card + HUD widget). Poster is the public
    # Cinemeta image URL; for a series the show name is the series title and `title` the episode.
    poster = meta.get("poster") or ""
    if typ == "series":
        cast_meta = caster.CastMeta(
            poster=poster, series_title=name, season=season or 0, episode=episode or 0
        )
    else:
        cast_meta = caster.CastMeta(poster=poster)
    return _auto_play(
        cfg, args, opts, typ, video_id, title, imdb_id, season, episode, selection, cast_meta,
        name=name,
    )  # fmt: skip


def _auto_play(
    cfg: Config,
    args: argparse.Namespace,
    opts: PlayOpts,
    typ: str,
    video_id: str,
    title: str,
    imdb_id: str,
    season: int | None,
    episode: int | None,
    selection: str,
    cast_meta: caster.CastMeta | None = None,
    *,
    name: str | None = None,
) -> int:
    """Delegate to `headless_play` (stream resolve + play/cast + success JSON)."""
    return headless_play.auto_play(
        cfg,
        args,
        opts,
        typ,
        video_id,
        title,
        imdb_id,
        season,
        episode,
        selection,
        cast_meta,
        name=name,
    )


def _reconcile_cast_session(cfg: Config) -> None:
    """Fold the receiver's real position into history before deciding what to continue.

    A fire-and-return cast returns as soon as the TV has the media, so nothing ever observes
    where it got to: history holds `duration = 0`, which can never read as finished, and `-c`
    would keep proposing the episode already watched. Ask the receiver once — the same
    `update_from_receiver` merge `--stop`/`--status` do, at the one call site where the answer
    changes a decision. No discovery: without a live session this costs nothing, and a TV that
    was switched off before any poll simply leaves no position (an honest gap; the alternative
    is inventing a duration, which ADR 0028 §6 rules out)."""
    device = state.cast_session_device()
    if not device:
        return
    info = caster.status(device)
    state.update_from_receiver(
        cfg, device, info.get("position") or 0.0, info.get("duration") or 0.0,
        title=info.get("title"),
    )  # fmt: skip


def _run_auto_resume(
    cfg: Config, args: argparse.Namespace, opts: PlayOpts, query: str, typ: str | None = None
) -> int:
    """Headless resume (`--json -c`): pick a history entry by normalized title (or the most
    recent when no query) and replay it, with no fzf. `typ` narrows to one content type.
    A FINISHED series episode advances: `-c "show"` after the S01E04 credits casts S01E05.

    Both the candidate list (`state.resumable`) and the decision (`series.next_up`) are the
    shared ones the interactive `-c` uses (ADR 0029). They used to be local to this function,
    which is how the two paths drifted apart — and how a search term could surface a
    half-watched episode from weeks ago over a binge finished minutes earlier."""
    _reconcile_cast_session(cfg)
    entries = state.resumable(cfg, typ=typ)
    entry: HistoryEntry | None = None
    if query:
        q = api.norm_text(query)
        entry = next((e for e in entries if api.norm_text(e.get("title", "")) == q), None)
    elif entries:
        entry = entries[0]
    if entry is None:
        message = f"nessuna cronologia per «{query}»" if query else "cronologia vuota"
        _emit_json({"ok": False, "error": "no_result", "message": message})
        return 1
    typ = entry.get("type", "movie")
    nu = series.next_up(cfg, entry)
    if nu.selection == "completed":
        _emit_json(
            {
                "ok": False,
                "error": "series_completed",
                "message": f"«{entry.get('title', '?')}» è finita: nessun episodio dopo "
                f"S{entry.get('season', 0):02d}E{entry.get('episode', 0):02d}",
            }
        )
        return 1
    if nu.selection == "next" and nu.video is not None:
        nxt = nu.video
        series_id = entry.get("series_id") or entry["video_id"]
        return _auto_play(
            cfg, args, opts, "series", nxt["id"],
            display_title(entry.get("title", "?"), nxt),
            series_id, nxt.get("season"), nxt.get("episode"), "next",
            name=entry.get("title"),
        )  # fmt: skip
    return _auto_play(
        cfg, args, opts, typ, entry["video_id"],
        display_title(entry.get("title", "?"), series.entry_video(entry)),
        entry.get("series_id") or entry["video_id"],
        entry.get("season") or None, entry.get("episode") or None, "resume",
        name=entry.get("title"),
    )  # fmt: skip


def _headless_device(cfg: Config, args: argparse.Namespace) -> str | None:
    """Resolve the cast device for a lifecycle action (--stop/--status), or None (emitting a
    device_not_found JSON) when no Chromecast can be resolved without a picker."""
    try:
        return _resolve_device(cfg, headless=True, prefer=args.device)
    except CastUnavailable as e:
        _emit_json({"ok": False, "error": "device_not_found", "message": str(e)})
        return None


def _run_stop(cfg: Config, args: argparse.Namespace) -> int:
    """`--json --stop`: stop the cast on the resolved device."""
    # A mirror cast is self-contained (sender + headless mpv + null sink, no DMR session):
    # tear it down first, independent of DMR device resolution.
    mirror_stopped = mirror.stop()
    device = _headless_device(cfg, args)
    if device is None:
        if mirror_stopped:
            _emit_json({"ok": True, "action": "stop", "device": None, "error": None})
            return 0
        return 1
    # Read the receiver position BEFORE stopping: it's the resume point of a
    # fire-and-return cast, merged into history via the cast session below.
    st = caster.status(device)
    ok = caster.stop(device)
    # A castbridge cast lives in the daemon (not in catt), so stop that session too.
    if bridge.bridge_available():
        ok = bridge.stop(device) or ok
    # Also tear down a detached Tier-2 remux server + its temp file, if one is serving.
    # Its success counts: with the TV already unreachable, reclaiming the server and the
    # multi-GB temp file is a real stop, not a failure.
    ok = remux.stop(device) or ok
    ok = ok or mirror_stopped
    state.update_from_receiver(
        cfg, device, st.get("position") or 0.0, st.get("duration") or 0.0,
        title=st.get("title"), clear=True,
    )  # fmt: skip
    _emit_json(
        {"ok": ok, "action": "stop", "device": device, "error": None if ok else "stop_failed"}
    )
    return 0 if ok else 1


def _run_control(cfg: Config, args: argparse.Namespace) -> int:
    """`--json --pause/--resume/--seek S`: media control on the resolved device.
    Prefers the castbridge daemon (`bridge.control`, the capability was implemented but
    had no caller); falls back to `catt play/pause/seek` so control works on catt-only
    sessions too."""
    device = _headless_device(cfg, args)
    if device is None:
        return 1
    if args.seek is not None:
        action, cmd, value = "seek", "seek", float(args.seek)
        catt_args = ["seek", str(int(args.seek))]
    elif args.pause:
        action, cmd, value = "pause", "pause", 0.0
        catt_args = ["pause"]
    else:
        action, cmd, value = "resume", "play", 0.0
        catt_args = ["play"]
    ok = bridge.bridge_available() and bridge.control(device, cmd, value)
    if not ok:
        res = util.run_cmd(["catt", "-d", device, *catt_args], timeout=util.CATT_INFO_TIMEOUT)
        ok = bool(res and res.returncode == 0)
    _emit_json(
        {
            "ok": ok,
            "action": action,
            "device": device,
            "seek": args.seek,
            "error": None if ok else "control_failed",
        }
    )
    return 0 if ok else 1


def _run_volume(cfg: Config, args: argparse.Namespace) -> int:
    """`--json --volume N` without a title: set the receiver volume on the cast in
    progress (it used to require re-casting a whole title just to change volume)."""
    device = _headless_device(cfg, args)
    if device is None:
        return 1
    ok = caster.set_volume(device, args.volume)
    _emit_json(
        {
            "ok": ok,
            "action": "volume",
            "device": device,
            "volume": args.volume,
            "error": None if ok else "volume_failed",
        }
    )
    return 0 if ok else 1


def _run_status(cfg: Config, args: argparse.Namespace) -> int:
    """`--json --status`: report the receiver's current playback state."""
    device = _headless_device(cfg, args)
    if device is None:
        return 1
    st = caster.status(device)
    # Keep the fire-and-return resume point fresh: every status poll merges the receiver
    # position into the cast-session history entry (no-op without a session).
    state.update_from_receiver(
        cfg, device, st.get("position") or 0.0, st.get("duration") or 0.0,
        title=st.get("title"),
    )  # fmt: skip
    _emit_json({"ok": True, "action": "status", "device": device, **st, "error": None})
    return 0
