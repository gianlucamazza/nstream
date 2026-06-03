"""Command-line entry point: search/browse → pick (fzf) → play (mpv)."""

from __future__ import annotations

import argparse
import contextlib
import gzip
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast as typecast

from . import (
    __version__,
    api,
    explain,
    languages,
    log,
    preview,
    quality,
    settings,
    state,
    tracks,
    ui,
)
from .caster import CastUnavailable, cast
from .caster import resolve_device as _resolve_device
from .config import (
    Config,
    ConfigError,
    HistoryEntry,
    Meta,
    Stream,
    Subtitle,
    Video,
    config_path,
    load,
)
from .picker import fzf, fzf_key
from .player import play

# --browse keyword → Cinemeta catalog id.
CAT_MAP = {"popolari": "top", "nuovi": "year", "top": "imdbRating"}

_log = log.get_logger("cli")


@dataclass(frozen=True)
class PlayOpts:
    """Per-invocation playback preferences threaded through the flow."""

    auto: bool  # auto-pick the top stream (skip the stream menu)
    cast: bool  # send playback to a Chromecast (catt) instead of mpv
    sub_mode: str | None  # None = no subs, "auto" = pick preferred lang, "menu" = fzf
    sub_lang: str | None  # force this language for sub_mode="auto"
    history: bool  # record/resume watch history
    autoplay: bool  # offer the next-episode overlay for series
    cast_choose: bool = False  # force the device picker (explicit "cast this" action)


def _clear() -> None:
    """Wipe the terminal (screen + scrollback) so menus and mpv output never pile up.
    No-op when stdout isn't a TTY (tests, pipes) so non-interactive runs stay clean."""
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[H\x1b[2J\x1b[3J")
        sys.stdout.flush()


# Active theme (glyphs + palette), resolved once in main() after config load and used by
# the label builders. Sensible defaults keep direct calls (e.g. in tests) self-contained.
_GLYPHS: ui.Glyphs = ui.PORTABLE
_PAL: ui.Palette = ui.palette(ui.Caps())


def _init_theme(cfg: Config) -> None:
    global _GLYPHS, _PAL
    caps = ui.detect_caps(cfg)
    ui.set_active_caps(caps)
    _GLYPHS = ui.glyphs(caps)
    _PAL = ui.palette(caps)


def meta_label(m: Meta) -> str:
    g, pal = _GLYPHS, _PAL
    icon = g.series if m.get("type") == "series" else g.movie
    info = m.get("releaseInfo", "")
    year = f"  {ui.ansi(f'({info})', pal.dim)}" if info else ""
    label = f"{icon}  {ui.ansi(m.get('name', '?'), pal.accent)}{year}"
    # Cheap hint from the slim catalog (year only): flag titles from a future year.
    # Same-year-but-unreleased titles are caught precisely at selection time.
    yr = re.match(r"(\d{4})", str(info))
    if yr and int(yr.group(1)) > datetime.now(UTC).year:
        label += "  " + ui.ansi(f"· {g.movie} in uscita", pal.warn)
    return label


def stream_label(s: Stream, info: quality.StreamInfo | None = None) -> str:
    name = (s.get("name") or "").replace("\n", " ")
    title = (s.get("title") or "").replace("\n", " · ")
    base = f"{name}  |  {title}"[:200]
    if info is None:
        return base
    tags = []
    if info.resolution:
        tags.append(f"{info.resolution}p")
    if info.codec:
        tags.append(info.codec)
    if info.dv:
        tags.append("DV")
    elif info.hdr:
        tags.append("HDR")
    if info.source:
        tags.append(info.source)
    if info.audio:
        tags.append(info.audio.upper())
    if info.languages:
        tags.append("/".join(sorted(info.languages)))
    if info.size_gb:
        tags.append(f"{info.size_gb:.1f}G")
    prefix = (_GLYPHS.cached if info.cached else " ") + " " + " ".join(tags)
    # Truncate the plain string first, then colour only the fixed-width prefix region so
    # the alignment is exact and no ANSI escape is ever cut by the 200-char cap.
    plain = f"{prefix:36s} {base}"[:200]
    return ui.ansi(plain[:36], _PAL.good if info.cached else _PAL.dim) + plain[36:]


def episode_label(v: Video) -> str:
    g, pal = _GLYPHS, _PAL
    tag = ui.ansi(f"S{v.get('season', 0):02d}E{v.get('episode', 0):02d}", pal.dim)
    return f"{g.series}  {tag}  {v.get('name', '')}".rstrip()


def history_label(e: HistoryEntry) -> str:
    pal = _PAL
    title = ui.ansi(e.get("title", "?"), pal.secondary)
    if e.get("type") == "series" and e.get("season"):
        title += "  " + ui.ansi(f"S{e.get('season', 0):02d}E{e.get('episode', 0):02d}", pal.dim)
    dur = e.get("duration") or 0.0
    pos = e.get("position", 0.0)
    if dur:
        bar = ui.progress_bar(pos, dur, width=12, caps=ui.active_caps())
        title += "  " + ui.ansi(f"{bar} {pos / dur * 100:.0f}%", pal.accent)
    return title


def display_title(name: str, video: Video | None) -> str:
    """The media title shown by mpv (OSC, window, taskbar)."""
    if video is None:
        return name
    label = f"{name} · S{video.get('season', 0):02d}E{video.get('episode', 0):02d}"
    epname = video.get("name")
    return f"{label} · {epname}" if epname else label


# --- subtitles ------------------------------------------------------------


def _download_subtitle(sub: Subtitle, work_dir: str) -> str | None:
    url = sub.get("url")
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": api.UA})
        with urllib.request.urlopen(req, timeout=api.TIMEOUT) as resp:
            raw = resp.read()
    except OSError:
        print("nstream: download sottotitolo fallito", file=sys.stderr)
        return None
    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        with contextlib.suppress(OSError):
            raw = gzip.decompress(raw)
    # Written into the per-play temp dir so it is cleaned up with everything else.
    fd, path = tempfile.mkstemp(prefix=f"{sub.get('lang', 'sub')}-", suffix=".srt", dir=work_dir)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path


def pick_subtitles(
    cfg: Config,
    typ: str,
    video_id: str,
    work_dir: str,
    *,
    mode: str = "auto",
    lang: str | None = None,
) -> tuple[str, ...]:
    try:
        subs = api.subtitles(cfg, typ, video_id)
    except api.NetworkError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return ()
    if not subs:
        print("nstream: nessun sottotitolo", file=sys.stderr)
        return ()
    langs = [lang] if lang else cfg.subtitle_langs
    pref = {code: i for i, code in enumerate(langs)}
    subs.sort(key=lambda s: pref.get(s.get("lang", ""), len(pref)))
    if mode == "menu":
        items = [(f"{s.get('lang', '?'):5s} {s.get('id', '')}", s) for s in subs]
        chosen = fzf(items, "sottotitoli> ")
    else:  # auto: take the best preferred-language track, else skip silently
        chosen = subs[0] if subs[0].get("lang", "") in pref else None
        if chosen is None:
            print("nstream: nessun sottotitolo nelle lingue preferite", file=sys.stderr)
    if not chosen:
        return ()
    path = _download_subtitle(chosen, work_dir)
    return (path,) if path else ()


# --- pre-play audio/subtitle track menu ----------------------------------


def track_label(t: tracks.Track) -> str:
    parts = [t.lang or "und"]
    if t.codec:
        parts.append(t.codec)
    if t.channels:
        parts.append(f"{t.channels}ch")
    if t.title:
        parts.append(f'"{t.title}"')
    return " · ".join(parts)


def _audio_summary(aid: int | None, tr: tracks.Tracks) -> str:
    if aid is None:
        return "automatico (lingua preferita)"
    t = next((a for a in tr.audio if a.id == aid), None)
    return track_label(t) if t else f"traccia {aid}"


def _sub_summary(sid: int | str | None, sub_paths: tuple[str, ...], tr: tracks.Tracks) -> str:
    if sub_paths:
        return "OpenSubtitles (esterni)"
    if sid in (None, "no"):
        return "nessuno"
    t = next((s for s in tr.subs if s.id == sid), None)
    return track_label(t) if t else f"traccia {sid}"


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
    _PLAY, _AUDIO, _SUBS, _AUTO, _OPENSUBS = (object() for _ in range(5))
    while True:
        items: list[tuple[str, object]] = [
            (f"{_GLYPHS.play}  Avvia", _PLAY),
            (f"🔊 Audio: {_audio_summary(aid, tr)}", _AUDIO),
            (f"💬 Sottotitoli: {_sub_summary(sid, sub_paths, tr)}", _SUBS),
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
                got = pick_subtitles(cfg, typ, video_id, work_dir, mode="menu")
                if got:
                    sub_paths, sid = got, None
            else:
                sid, sub_paths = typecast("str | int", pick), ()


# --- flow ----------------------------------------------------------------


def _future_release(iso: str | None) -> datetime | None:
    """Parse a Cinemeta `released` ISO date; return it only if it's in the future."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt > datetime.now(UTC) else None


def _no_streams_message(cfg: Config, typ: str, video_id: str, title: str) -> str:
    """A specific 'not released yet' notice when a title has no streams, else generic."""
    released = _future_release(api.meta(cfg, typ, video_id).get("released"))
    if released:
        return f"🎬 «{title}» non ancora disponibile — uscita prevista il {released:%d/%m/%Y}"
    return f"nessuno stream disponibile per «{title}»"


def _pick_stream(
    cfg: Config, results: list[Stream], *, auto: bool, cast: bool = False
) -> Stream | None:
    """Rank and curate streams, then auto-pick the best or show an fzf menu (top N
    playable + a 'show all' entry that reveals the rest and the excluded ones ⚠).

    When `cast`, rank against the Chromecast receiver's profile (not the laptop GPU)
    and demote streams whose audio it can't decode (TrueHD/DTS/DTS-HD → silent)."""
    if not cfg.hw_filter:
        ranked = [(stream_label(s, quality.parse_stream(s)), s) for s in results]
        return results[0] if auto else fzf(ranked, "stream> ")

    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(cfg, cast_audio=cast)
    playable, excluded = quality.rank_streams(results, caps, spec)
    if excluded:
        reasons = ", ".join(sorted({r.reason for r in excluded if r.reason}))
        print(f"nstream: {len(excluded)} stream filtrati ({reasons})", file=sys.stderr)
    dupes = len(results) - len(playable) - len(excluded)
    if dupes > 0:
        print(f"nstream: {dupes} doppioni rimossi", file=sys.stderr)
    if auto:
        if playable:
            return playable[0].stream
        msg = (
            "nessuno stream compatibile col Chromecast (prova Tab o --local)"
            if cast
            else "nessuno stream supportato dall'hardware"
        )
        print(f"nstream: {msg}", file=sys.stderr)
        return None

    def _full() -> Stream | None:
        items = [(stream_label(r.stream, r.info), r.stream) for r in playable]
        items += [(f"⚠ {r.reason}  {stream_label(r.stream, r.info)}", r.stream) for r in excluded]
        return fzf(items, "stream> ")

    cap = cfg.max_streams
    if not cap or len(playable) + len(excluded) <= cap:
        return _full()  # nothing hidden → one flat menu
    _ALL = object()
    shown = playable[:cap]
    hidden = len(playable) - len(shown) + len(excluded)
    items: list[tuple[str, object]] = [(stream_label(r.stream, r.info), r.stream) for r in shown]
    items.append((f"↓ mostra tutti ({hidden} altri)", _ALL))
    chosen = fzf(items, "stream> ")
    if chosen is _ALL:
        return _full()
    return typecast("Stream | None", chosen)


def _cast_playable(cfg: Config, results: list[Stream]) -> list[quality.RankedStream]:
    """Streams the Chromecast can play (cast profile + Cast-compatible audio), ignoring
    the language filter so every available dub is offered for switching."""
    spec = quality.FilterSpec.from_config(cfg, cast_audio=True, lang_filter=False)
    playable, _ = quality.rank_streams(results, quality.cast_caps(), spec)
    return playable


def _cast_languages(cfg: Config, results: list[Stream]) -> tuple[str, ...]:
    """Audio languages available among Cast-compatible streams, preferred ones first."""
    langs = {
        lang for r in _cast_playable(cfg, results) for lang in r.info.languages if lang != "multi"
    }
    ordered = [lang for lang in cfg.audio_langs if lang in langs]
    ordered += sorted(langs - set(ordered))
    return tuple(ordered)


def _cast_resolver(cfg: Config, results: list[Stream]) -> Callable[[str], str | None]:
    """Return a fn picking the best Cast-compatible stream URL for a language, or None.
    Closes over the already-fetched `results` so switching needs no extra network call."""
    playable = _cast_playable(cfg, results)

    def resolve(lang: str) -> str | None:
        for r in playable:  # already ranked best-first
            if lang in r.info.languages:
                return r.stream.get("url")
        return None

    return resolve


def _resume_position(cfg: Config, video_id: str) -> float | None:
    """The position to resume from, or None if there's no usable resume point
    (no history entry, or the title is effectively finished — so we never restart
    at the very end when mpv was left paused at EOF with keep-open)."""
    entry = state.load_history(cfg).get(video_id)
    if not entry or state._watched(entry):
        return None
    start = entry.get("position")
    dur = entry.get("duration") or 0.0
    if start and dur > 0:
        return min(start, dur - 5)
    return start


def _audio_langs_of(cfg: Config, chosen: Stream) -> set[str] | None:
    """The audio languages actually in `chosen`, as canonical codes — for the auto-play
    guard. Best-effort: returns None when it can't tell (no preference set, or ffprobe
    missing/empty), so the caller never blocks playback on a probe failure. Skips the
    ffprobe entirely when the release is already tagged with a preferred language."""
    pref = set(cfg.audio_langs)
    if not pref:
        return None
    tagged = quality.parse_stream(chosen).languages
    if "multi" in tagged or (tagged & pref):
        return pref  # trust the tag for the common well-tagged case (no probe)
    tr = tracks.probe_tracks(chosen.get("url") or "")
    if tr.empty():
        return None  # unverifiable → don't block
    return {code for t in tr.audio if (code := languages.normalize(t.lang))}


def _play_video(
    cfg: Config,
    typ: str,
    video_id: str,
    title: str,
    opts: PlayOpts,
    *,
    auto: bool,
    next_label: str | None,
    on_save: Callable[[float, float], None] | None,
    reselect_on_wrong_audio: bool = True,
) -> tuple[str | None, bool]:
    """Resolve streams for one video, play it, persist progress. Returns
    (notice, advance): `notice` is a user-facing message to surface (no streams /
    not released yet) or None on success or a cancelled stream menu; `advance` is
    True when the next-episode overlay asked to continue.

    `auto` overrides `opts.auto` for this single video: the binge loop forces it
    True from the second episode on, so use `auto` (not `opts.auto`) here."""
    # Resolving streams (Torrentio + RD) can take a moment; without a menu to mask
    # the wait, say what's happening so the TUI doesn't look frozen.
    print(f"▶ {title} — cerco la sorgente migliore…", file=sys.stderr)
    results = api.streams(cfg, typ, video_id)
    if not results:
        notice = _no_streams_message(cfg, typ, video_id, title)
        print(f"nstream: {notice}", file=sys.stderr)
        return (notice, False)
    chosen = _pick_stream(cfg, results, auto=auto, cast=opts.cast)
    if not chosen:
        return (None, False)

    # Auto-play language guard (local mpv only): the auto-pick can be a file with no audio
    # in a preferred language (an untagged/mistagged foreign leak). Verify with ffprobe and,
    # rather than letting mpv silently fall back to the wrong dub, warn and (when interactive)
    # let the user pick another source. Cast keeps its own language UX.
    if auto and not opts.cast:
        avail = _audio_langs_of(cfg, chosen)
        if avail is not None and not (set(cfg.audio_langs) & avail):
            have = "/".join(sorted(avail)) or "?"
            print(
                f"nstream: nessuna traccia audio {','.join(cfg.audio_langs)} (disponibili: {have})",
                file=sys.stderr,
            )
            if reselect_on_wrong_audio:
                auto = False  # let choose_tracks give track control on the manual pick
                chosen = _pick_stream(cfg, results, auto=False, cast=opts.cast)
                if not chosen:
                    return (None, False)

    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="nstream-", dir=runtime) as work_dir:
        start = _resume_position(cfg, video_id) if opts.history else None
        name_line = next(iter((chosen.get("name") or "").splitlines()), "")
        # Resolve the cast device up front; if none is reachable on this LAN, degrade
        # gracefully to local mpv instead of failing (network may have changed).
        device = _resolve_cast_device(cfg, opts) if opts.cast else None
        if device is not None:
            print(f"▶ {title} — {name_line}", file=sys.stderr)
            pos, dur, advance = _play_on_cast(
                cfg, results, chosen, work_dir, device,
                typ=typ, video_id=video_id, title=title, opts=opts,
                start=start, next_label=next_label,
            )  # fmt: skip
        else:
            print(f"▶ {title} — {name_line}", file=sys.stderr)
            res = _play_on_mpv(
                cfg, chosen, work_dir,
                typ=typ, video_id=video_id, title=title, opts=opts,
                start=start, next_label=next_label, auto=auto,
            )  # fmt: skip
            if res is None:
                return (None, False)  # backed out of the track menu → return to the list
            pos, dur, advance = res
    _clear()  # drop mpv's exit frame/logs before returning to the menu
    # Only persist a resume we can reason about: a real duration is needed for the
    # watched/near-end logic, otherwise the entry would stick forever.
    if opts.history and on_save and pos > 0 and dur > 0:
        on_save(pos, dur)
    return (None, advance)


def _resolve_cast_device(cfg: Config, opts: PlayOpts) -> str | None:
    """Resolve a Chromecast for this play, or None to fall back to local mpv when the
    LAN has no reachable device (e.g. after a network change)."""
    try:
        return _resolve_device(cfg, choose=opts.cast_choose)
    except CastUnavailable as e:
        print(f"nstream: {e} — riproduco in locale", file=sys.stderr)
        _log.info("nessun Chromecast → fallback locale")
        return None


def _auto_subs(
    cfg: Config, typ: str, video_id: str, work_dir: str, opts: PlayOpts
) -> tuple[str, ...]:
    """Subtitle files for the no-menu paths (cast / --play / binge): the preferred-
    language OpenSubtitles track when subtitles were requested, else none."""
    if not opts.sub_mode:
        return ()
    return pick_subtitles(cfg, typ, video_id, work_dir, mode=opts.sub_mode, lang=opts.sub_lang)


def _play_on_cast(
    cfg: Config,
    results: list[Stream],
    chosen: Stream,
    work_dir: str,
    device: str,
    *,
    typ: str,
    video_id: str,
    title: str,
    opts: PlayOpts,
    start: float | None,
    next_label: str | None,
) -> tuple[float, float, bool]:
    """Cast the chosen stream. Offers an in-cast audio-language switch (re-cast a
    differently-dubbed release from the current position) when more than one language
    is available; the resolver closes over `results` so no extra fetch is needed."""
    # Cast can't drive embedded track ids (mpv-only); subtitles go to the TV as an
    # external file when requested, otherwise the receiver picks its own.
    sub_paths = _auto_subs(cfg, typ, video_id, work_dir, opts)
    cast_langs = _cast_languages(cfg, results)
    _log.info("cast '%s' → %s", title, device)
    return cast(
        cfg, title, chosen["url"],
        device=device, start=start, sub_paths=sub_paths, next_label=next_label,
        langs=cast_langs if len(cast_langs) > 1 else (),
        resolve_lang=_cast_resolver(cfg, results) if len(cast_langs) > 1 else None,
    )  # fmt: skip


def _play_on_mpv(
    cfg: Config,
    chosen: Stream,
    work_dir: str,
    *,
    typ: str,
    video_id: str,
    title: str,
    opts: PlayOpts,
    start: float | None,
    next_label: str | None,
    auto: bool,
) -> tuple[float, float, bool] | None:
    """Play locally in mpv. Returns (pos, dur, advance), or None if the user backed
    out of the pre-play track menu (so the caller returns to the list)."""
    audio_id: int | None = None
    sub_id: str | int | None = None
    if auto:
        sub_paths = _auto_subs(cfg, typ, video_id, work_dir, opts)
    else:
        sel = choose_tracks(cfg, chosen["url"], typ, video_id, work_dir)
        if sel is None:
            return None
        audio_id, sub_id, sub_paths = sel
    cast_ok = shutil.which("catt") is not None  # enable in-player Alt-C → TV
    pos, dur, signal = play(
        cfg, title, chosen["url"],
        start=start, sub_paths=sub_paths, audio_id=audio_id, sub_id=sub_id,
        next_label=next_label, cast_enabled=cast_ok, work_dir=work_dir,
    )  # fmt: skip
    if signal == "cast":  # Alt-C in mpv: move this playback to the TV from `pos`
        return _move_to_cast(cfg, title, chosen, pos, dur)
    return (pos, dur, signal == "next")


def _move_to_cast(
    cfg: Config, title: str, chosen: Stream, pos: float, dur: float
) -> tuple[float, float, bool]:
    """Hand the running mpv position over to a Chromecast (in-player Alt-C). Keeps the
    local pos/dur if no device resolves. Never auto-advances (the user is switching)."""
    try:
        device = _resolve_device(cfg, choose=True)
    except CastUnavailable as e:
        print(f"nstream: {e}", file=sys.stderr)
        return (pos, dur, False)
    pos, dur, _ = cast(cfg, title, chosen["url"], device=device, start=pos)
    return (pos, dur, False)


def _play_series(
    cfg: Config, series_id: str, name: str, eps: list[Video], start_video: Video, opts: PlayOpts
) -> str | None:
    """Play a series from `start_video`, auto-advancing through the overlay.
    Returns a notice (e.g. an episode with no streams) to surface, or None."""
    idx = next((i for i, v in enumerate(eps) if v.get("id") == start_video.get("id")), None)
    if idx is None:
        return None
    auto = opts.auto  # the first episode honours --play; binge episodes auto-pick
    binge = False  # True once we're auto-advancing unattended (no blocking reselection)
    while 0 <= idx < len(eps):
        video = eps[idx]
        video_id = video["id"]
        nxt = eps[idx + 1] if idx + 1 < len(eps) else None
        next_label = display_title(name, nxt) if (opts.autoplay and nxt is not None) else None

        def on_save(pos: float, dur: float, vid: str = video_id, v: Video = video) -> None:
            state.save_entry(
                cfg, state.make_entry(vid, name, "series", pos, dur, series_id=series_id, video=v)
            )

        notice, advance = _play_video(
            cfg, "series", video_id, display_title(name, video), opts,
            auto=auto, next_label=next_label, on_save=on_save,
            reselect_on_wrong_audio=not binge,  # binge advances warn-and-proceed, don't block
        )  # fmt: skip
        if notice:
            return notice
        if not advance or nxt is None:
            return None
        idx += 1
        auto = True
        binge = True
        print(f"▶ Carico {display_title(name, eps[idx])}…", file=sys.stderr)
    return None


def play_meta(cfg: Config, meta: Meta, opts: PlayOpts) -> str | None:
    """Play a title; returns a notice to show above the list, or None."""
    typ = meta.get("type", "movie")
    name = meta.get("name", "nstream")
    if typ != "series":
        movie_id = meta["id"]

        def on_save(pos: float, dur: float) -> None:
            state.save_entry(cfg, state.make_entry(movie_id, name, typ, pos, dur))

        notice, _ = _play_video(
            cfg, typ, movie_id, display_title(name, None), opts,
            auto=opts.auto, next_label=None, on_save=on_save,
        )  # fmt: skip
        return notice

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
        chosen = fzf_key(items, "episodio> ", header=header or _pick_hint(opts), preview=ep_preview)
        if not chosen:
            return None
        key, start_video = chosen
        header = _play_series(cfg, meta["id"], name, eps, start_video, _apply_key(opts, key))


def _entry_video(entry: HistoryEntry) -> Video | None:
    if entry.get("type") != "series":
        return None
    return {"season": entry.get("season", 0), "episode": entry.get("episode", 0)}


def play_history(cfg: Config, entry: HistoryEntry, opts: PlayOpts) -> str | None:
    """Resume from a history entry; returns a notice to show, or None."""
    typ = entry.get("type", "movie")
    name = entry.get("title", "nstream")
    series_id = entry.get("series_id", "")
    # Resume a series and keep bingeing the rest of the season.
    if typ == "series" and series_id and opts.autoplay:
        eps = api.episodes(cfg, series_id)
        cur = next((v for v in eps if v.get("id") == entry["video_id"]), None)
        if cur is not None:
            return _play_series(cfg, series_id, name, eps, cur, opts)

    video_id = entry["video_id"]

    def on_save(pos: float, dur: float) -> None:
        state.save_entry(
            cfg,
            state.make_entry(
                video_id,
                name,
                typ,
                pos,
                dur,
                series_id=series_id,
                season=entry.get("season", 0),
                episode=entry.get("episode", 0),
            ),  # fmt: skip
        )

    notice, _ = _play_video(
        cfg, typ, video_id, display_title(name, _entry_video(entry)), opts,
        auto=opts.auto, next_label=None, on_save=on_save,
    )  # fmt: skip
    return notice


def _pick_hint(opts: PlayOpts) -> str:
    """Discoverability line for the leaf lists: Tab flips the play mode, Alt-C casts."""
    tab = "Tab: scegli sorgente/tracce" if opts.auto else "Tab: avvia al volo"
    return f"{tab}  ·  Alt-C: casta sul TV"


def _apply_key(opts: PlayOpts, key: str) -> PlayOpts:
    """Map a leaf-list selection key to per-pick options: Alt-C casts this title
    (forcing the device picker); Tab flips auto↔manual; Enter keeps the default."""
    if key == "alt-c":
        return replace(opts, cast=True, cast_choose=True)
    return replace(opts, auto=opts.auto ^ (key == "tab"))


def _meta_preview(m: Meta) -> str | None:
    """The `__preview` token for a title row (poster + metadata pane), or None."""
    vid = m.get("id")
    return f"title {m.get('type', 'movie')} {vid}" if vid else None


def _entry_preview(e: HistoryEntry) -> str | None:
    """Preview token for a continue-watching row: the episode for a series, else the title."""
    if e.get("type") == "series" and e.get("series_id"):
        return f"episode {e['series_id']} {e.get('season', 0)} {e.get('episode', 0)}"
    vid = e.get("video_id")
    return f"title {e.get('type', 'movie')} {vid}" if vid else None


def _pick_meta(items: list[tuple[str, Meta]], cfg: Config, opts: PlayOpts) -> int:
    """Loop the title list: play a pick, then return here. ESC leaves to the caller
    (HOME or the shell). A notice from playback is shown as the fzf header next time.
    Enter plays with the default mode; Tab flips auto↔manual for that pick (series
    defer the choice to the episode picker)."""
    header: str | None = None
    while True:
        chosen = fzf_key(
            items, "titolo> ", header=header or _pick_hint(opts), preview=_meta_preview
        )
        if not chosen:
            return 0
        key, meta = chosen
        # Series defer auto/manual to the episode picker, but Alt-C (cast) still applies.
        sel = replace(opts, cast=True, cast_choose=True) if key == "alt-c" else opts
        if meta.get("type") != "series":
            sel = _apply_key(opts, key)
        header = play_meta(cfg, meta, sel)


def run_search(cfg: Config, query: str, opts: PlayOpts) -> int:
    metas = api.search(cfg, query)
    if not metas:
        print("nstream: nessun risultato", file=sys.stderr)
        return 1
    return _pick_meta([(meta_label(m), m) for m in metas], cfg, opts)


def run_browse(cfg: Config, cat: str, opts: PlayOpts) -> int:
    metas = api.browse(cfg, cat)  # movies + series, fetched concurrently
    if not metas:
        print("nstream: catalogo vuoto", file=sys.stderr)
        return 1
    return _pick_meta([(meta_label(m), m) for m in metas], cfg, opts)


def run_explain(cfg: Config, query: str) -> int:
    """`--explain`: search → pick a title (and episode, for series) → print WHY the
    auto-pick won (ranking table for local + cast profiles, plus the audio decision).
    Read-only: never plays or casts."""
    metas = api.search(cfg, query)
    if not metas:
        print("nstream: nessun risultato", file=sys.stderr)
        return 1
    meta = fzf([(meta_label(m), m) for m in metas], "titolo> ", preview=_meta_preview)
    if meta is None:
        return 0
    typ = meta.get("type", "movie")
    video_id = meta["id"]
    title = meta.get("name", "?")
    if typ == "series":
        eps = api.episodes(cfg, video_id)
        if not eps:
            print(f"nstream: nessun episodio per «{title}»", file=sys.stderr)
            return 1
        v = fzf([(episode_label(e), e) for e in eps], "episodio> ")
        if v is None:
            return 0
        video_id = v["id"]
        title = display_title(title, v)
    results = api.streams(cfg, typ, video_id)
    print(f"\n# nstream --explain · {title}\n")
    print(explain.explain_streams(cfg, results, cast=False))
    print()
    print(explain.explain_streams(cfg, results, cast=True))
    print()
    print(explain.explain_audio(cfg, explain.auto_pick(cfg, results, cast=False)))
    return 0


def run_continue(cfg: Config, opts: PlayOpts) -> int:
    """`-c`: resume from history, returning to the list after each play (ESC exits)."""
    entries = state.recent(cfg)
    if not entries:
        print("nstream: cronologia vuota", file=sys.stderr)
        return 0
    header: str | None = None
    while True:
        items = [(history_label(e), e) for e in entries]
        chosen = fzf_key(
            items, "continua> ", header=header or _pick_hint(opts), preview=_entry_preview
        )
        if chosen is None:
            return 0
        key, entry = chosen
        header = play_history(cfg, entry, _apply_key(opts, key))
        entries = state.recent(cfg)  # reflect updated positions, then re-show


# Home-menu action kinds (the value half of an fzf item; history entries are dicts).
_SEARCH = "search"
_BROWSE = "browse"
_SETTINGS = "settings"


def run_home(cfg: Config, opts: PlayOpts) -> int:
    """The TUI home: continue-watching + search + browse + settings, in one menu.
    Loops until the user backs out (ESC). This is the rich entry surface — the
    desktop/fuzzel launcher only opens it; no UI logic lives in fuzzel."""
    notice: str | None = None
    while True:
        recent = state.recent(cfg) if opts.history else []
        g = _GLYPHS
        items: list[tuple[str, object]] = [(history_label(e), e) for e in recent]
        items += [
            (f"{g.search}  Cerca…", (_SEARCH, "")),
            (f"{g.fire}  Popolari", (_BROWSE, "popolari")),
            (f"{g.new}  Novità", (_BROWSE, "nuovi")),
            (f"{g.star}  Top IMDb", (_BROWSE, "top")),
            (f"{g.gear}  Impostazioni", (_SETTINGS, "")),
        ]

        def home_preview(value: object) -> str | None:
            # Action rows (tuples) have no preview; continue-watching entries (dicts) do.
            return (
                None
                if isinstance(value, tuple)
                else _entry_preview(typecast("HistoryEntry", value))
            )

        # The Tab hint only applies to the continue-watching rows.
        header = notice or (_pick_hint(opts) if recent else None)
        chosen = fzf_key(items, "nstream> ", header=header, preview=home_preview)
        notice = None
        if chosen is None:
            return 0
        key, value = chosen
        if not isinstance(value, tuple):  # a continue-watching entry
            notice = play_history(cfg, typecast("HistoryEntry", value), _apply_key(opts, key))
            continue
        kind, value = value
        if kind == _SEARCH:
            try:
                query = input("cerca> ").strip()
            except EOFError:
                return 0
            if query:
                run_search(cfg, query, opts)
        elif kind == _BROWSE:
            run_browse(cfg, CAT_MAP[typecast(str, value)], opts)
        elif kind == _SETTINGS:
            settings.run_settings(cfg)
            cfg = load()  # pick up any change for the next loop


def _dispatch(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    _clear()  # start the interactive session on a clean screen (drop launcher banner)
    if args.cont:
        return run_continue(cfg, opts)
    if args.browse:
        return run_browse(cfg, CAT_MAP[args.browse], opts)
    query = " ".join(args.query)
    if args.explain:
        if not query:
            print("nstream: --explain richiede un titolo da cercare", file=sys.stderr)
            return 2
        return run_explain(cfg, query)
    if query:
        return run_search(cfg, query, opts)
    return run_home(cfg, opts)


def _sub_options(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.sub_lang:
        return ("auto", args.sub_lang)
    if args.sub_menu:
        return ("menu", None)
    if args.subs:
        return ("auto", None)
    return (None, None)


def _ensure_config() -> Config:
    """Load config, running first-run onboarding if it's missing."""
    try:
        return load()
    except ConfigError:
        if not config_path().exists():
            settings.onboard()  # prompts for the RD token, writes a minimal config
            return load()
        raise


def main() -> int:
    # Hidden fast path: fzf invokes `nstream __preview …` per focused row. Handle it
    # before argparse (it must stay lightweight and not collide with the query positional).
    if sys.argv[1:2] == ["__preview"]:
        return preview.run_preview(sys.argv[2:])

    parser = argparse.ArgumentParser(
        prog="nstream",
        description="Native Stremio-like client (Cinemeta + Torrentio + Real-Debrid + mpv).",
    )
    parser.add_argument("query", nargs="*", help="titolo da cercare (altrimenti chiede)")
    parser.add_argument(
        "--play",
        action="store_true",
        help="forza la riproduzione automatica (anche se disattivata)",
    )
    parser.add_argument(
        "--cast", action="store_true", help="manda lo stream a un Chromecast (catt) invece di mpv"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="forza la riproduzione locale in mpv (anche se il default è cast)",
    )
    parser.add_argument(
        "--subs", action="store_true", help="sottotitoli automatici nella lingua preferita"
    )
    parser.add_argument("--sub-menu", action="store_true", help="scegli i sottotitoli a mano (fzf)")
    parser.add_argument("--sub-lang", metavar="CODE", help="lingua sottotitoli da auto-scegliere")
    parser.add_argument(
        "--browse", nargs="?", const="popolari", choices=list(CAT_MAP),
        help="sfoglia un catalogo Cinemeta invece di cercare (default: popolari)",
    )  # fmt: skip
    parser.add_argument(
        "-c", "--continue", dest="cont", action="store_true",
        help="riprendi dalla cronologia (continua a guardare)",
    )  # fmt: skip
    parser.add_argument("--no-history", action="store_true", help="non salvare la cronologia")
    parser.add_argument(
        "--no-autoplay", action="store_true", help="non proporre il prossimo episodio"
    )
    parser.add_argument("--settings", action="store_true", help="apri il menu impostazioni")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="spiega perché uno stream/audio verrebbe scelto (non riproduce)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="log verboso su stderr (oltre al file di log)"
    )
    parser.add_argument("--version", action="version", version=f"nstream {__version__}")
    args = parser.parse_args()

    log.setup_logging(args.debug or bool(os.environ.get("NSTREAM_DEBUG")))
    _log.info("nstream %s avvio (cast=%s)", __version__, args.cast or "")
    _log.debug("args: %r", vars(args))

    try:
        cfg = _ensure_config()
    except ConfigError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return 2

    _init_theme(cfg)

    if args.settings:
        settings.run_settings(cfg)
        return 0

    sub_mode, sub_lang = _sub_options(args)
    opts = PlayOpts(
        auto=cfg.auto_play or args.play,  # default mode; Tab flips it per pick
        cast=(cfg.prefer_cast or args.cast) and not args.local,
        sub_mode=sub_mode,
        sub_lang=sub_lang,
        history=cfg.history_enabled and not args.no_history,
        autoplay=cfg.autoplay and not args.no_autoplay,
    )
    try:
        return _dispatch(cfg, args, opts)
    except api.NetworkError as e:
        _log.warning("network: %s", e)
        print(f"nstream: {e}", file=sys.stderr)
        return 1


def _entry() -> None:
    # Configure logging before anything else so a crash in main() is captured even
    # when nstream runs inside the foot launcher (where the traceback would scroll away).
    log.setup_logging(bool(os.environ.get("NSTREAM_DEBUG")))
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
    except Exception:
        _log.exception("crash non gestito")
        print(f"nstream: errore inatteso — dettagli in {log.log_path()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _entry()
