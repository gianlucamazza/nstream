"""Command-line entry point: search/browse → pick (fzf) → play (mpv)."""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import cast

from . import __version__, api, quality, settings, state, tracks
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

# --browse keyword → Cinemeta catalog id.
CAT_MAP = {"popolari": "top", "nuovi": "year", "top": "imdbRating"}


@dataclass(frozen=True)
class PlayOpts:
    """Per-invocation playback preferences threaded through the flow."""

    auto: bool  # auto-pick the top stream (skip the stream menu)
    sub_mode: str | None  # None = no subs, "auto" = pick preferred lang, "menu" = fzf
    sub_lang: str | None  # force this language for sub_mode="auto"
    history: bool  # record/resume watch history
    autoplay: bool  # offer the next-episode overlay for series


def fzf[T](items: list[tuple[str, T]], prompt: str, *, header: str | None = None) -> T | None:
    """Pick one of (label, value) pairs via fzf. Returns the value or None.

    `header` shows a transient notice above the list (e.g. why a title couldn't
    play) — it survives the menu reopening, unlike a stderr line that scrolls away.
    A single item is returned directly only when there's nothing to announce."""
    if not items:
        return None
    if len(items) == 1 and header is None:
        return items[0][1]
    # Hidden leading index lets labels repeat without ambiguity.
    lines = "".join(f"{i}\t{label}\n" for i, (label, _) in enumerate(items))
    cmd = ["fzf", "--prompt", prompt, "--with-nth", "2..",
           "--delimiter", "\t", "--no-sort", "--reverse", "--height", "80%"]  # fmt: skip
    if header:
        cmd += ["--header", header]
    try:
        proc = subprocess.run(cmd, input=lines, capture_output=True, text=True)
    except FileNotFoundError:
        print("nstream: fzf non trovato", file=sys.stderr)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return items[int(proc.stdout.split("\t", 1)[0])][1]


def meta_label(m: Meta) -> str:
    info = m.get("releaseInfo", "")
    label = f"{m.get('type', '?'):6s} {m.get('name', '?')}  ({info})"
    # Cheap hint from the slim catalog (year only): flag titles from a future year.
    # Same-year-but-unreleased titles are caught precisely at selection time.
    year = re.match(r"(\d{4})", str(info))
    if year and int(year.group(1)) > datetime.now(UTC).year:
        label += "  · 🎬 in uscita"
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
    if info.size_gb:
        tags.append(f"{info.size_gb:.1f}G")
    prefix = ("✓" if info.cached else " ") + " " + " ".join(tags)
    return f"{prefix:28s} {base}"[:200]


def history_label(e: HistoryEntry) -> str:
    title = e.get("title", "?")
    if e.get("type") == "series" and e.get("season"):
        title += f"  S{e.get('season', 0):02d}E{e.get('episode', 0):02d}"
    dur = e.get("duration") or 0.0
    pct = f"  · {e.get('position', 0.0) / dur * 100:.0f}%" if dur else ""
    return f"{title}{pct}"


def display_title(name: str, video: Video | None) -> str:
    """The media title shown by mpv (OSC, window, taskbar)."""
    if video is None:
        return name
    label = f"{name} · S{video.get('season', 0):02d}E{video.get('episode', 0):02d}"
    epname = video.get("name")
    return f"{label} · {epname}" if epname else label


def _fmt_time(sec: float) -> str:
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


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
        chosen = fzf(items, "sottotitolo> ")
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
            ("▶ Avvia", _PLAY),
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
                aid = cast(int, pick)
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
                sid, sub_paths = cast("str | int", pick), ()


# --- playback + position tracking ----------------------------------------


def _track_position(
    sock_path: str,
    holder: dict[str, float],
    proc: subprocess.Popen,
    resume_msg: str | None = None,
) -> None:
    """Observe mpv's time-pos/duration over the IPC socket; record the last values."""
    sock: socket.socket | None = None
    for _ in range(50):
        if proc.poll() is not None:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(sock_path)
            break
        except OSError:
            sock = None
            time.sleep(0.1)
    if sock is None:
        return
    try:
        for cid, prop in ((1, "time-pos"), (2, "duration")):
            sock.sendall(f'{{"command":["observe_property",{cid},"{prop}"]}}\n'.encode())
        if resume_msg:
            payload = json.dumps({"command": ["show-text", resume_msg, 4000]})
            sock.sendall(payload.encode() + b"\n")
        # Time out recv so we notice mpv exiting promptly and the thread joins
        # cleanly — otherwise a blocked recv could outlive mpv and lose the last
        # observed position.
        sock.settimeout(0.5)
        buf = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                if proc.poll() is not None:
                    break
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("event") == "property-change" and msg.get("data") is not None:
                    name = msg.get("name")
                    if name in ("time-pos", "duration"):
                        holder["position" if name == "time-pos" else "duration"] = float(
                            msg["data"]
                        )
    except OSError:
        pass
    finally:
        sock.close()


def _mpv_conf_has(option: str) -> bool:
    """True if the user's mpv.conf already sets `option` (so we must not override it)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    candidates = (
        os.path.join(base, "mpv", "mpv.conf"),
        os.path.expanduser("~/.mpv/mpv.conf"),
    )
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            s = line.strip()
            if s and not s.startswith("#") and s.split("=", 1)[0].strip() == option:
                return True
    return False


def _mpv_conf_get(option: str) -> str | None:
    """The value the user's mpv.conf sets for `option`, or None if unset.
    Quotes are stripped so `hwdec="auto"` and `hwdec=auto` read the same."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    candidates = (
        os.path.join(base, "mpv", "mpv.conf"),
        os.path.expanduser("~/.mpv/mpv.conf"),
    )
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                key, _, val = s.partition("=")
                if key.strip() == option:
                    return val.strip().strip("'\"")
    return None


def _user_overrides(cfg: Config, option: str) -> bool:
    """True if the user already sets `option` via mpv_args or mpv.conf."""
    return any(a.startswith(f"--{option}") for a in cfg.mpv_args) or _mpv_conf_has(option)


# mpv's "auto" hwdec family: ambiguous choices that probe several methods. On a
# vulkan render context mpv 0.41+ prefers (experimental, often unsupported) Vulkan
# decode and then CUDA before VAAPI — noisy and software-bound on Intel iGPUs.
_AUTO_HWDEC = {"auto", "auto-safe", "auto-copy", "auto-safe-copy"}


def _hwdec_defaults(cfg: Config) -> list[str]:
    """Choose mpv's hwdec, upgrading the ambiguous `auto` family to the GPU's real
    method (VAAPI, detected) so mpv doesn't probe unsupported Vulkan decode / missing
    CUDA. An explicit `--hwdec` in mpv_args, or a concrete method in mpv.conf, is
    always respected; nstream's CLI flag overrides mpv.conf only to pin `auto`→vaapi."""
    if any(a.startswith("--hwdec") for a in cfg.mpv_args):
        return []  # explicit per-app override → defer entirely
    conf = _mpv_conf_get("hwdec")
    method = conf if conf is not None else cfg.hwdec
    if not method:
        return []  # decoding left to mpv defaults / explicitly disabled
    if method in _AUTO_HWDEC:
        detected = quality.preferred_hwdec(quality.detect_caps())
        if detected:
            return [f"--hwdec={detected}"]
        return [] if conf is not None else [f"--hwdec={method}"]
    # Concrete method: respect mpv.conf as-is; inject only nstream's own config value.
    return [] if conf is not None else [f"--hwdec={method}"]


# mpv log modules that spam the terminal of a media frontend (track list +
# decoder/demuxer/driver warnings). cplayer=warn drops the track list while the
# progress status line (a separate mechanism) survives; the rest hide ffmpeg/mkv
# warnings. Real errors (level error/fatal) still print.
_QUIET_MSG_LEVEL = (
    "cplayer=warn,ffmpeg=error,ffmpeg/video=error,ffmpeg/audio=error,mkv=error,ad=error,vd=error"  # noqa: E501
)


def _quiet_defaults(cfg: Config) -> list[str]:
    """Quieten mpv's console unless the user manages msg-level themselves."""
    if not cfg.mpv_quiet or _user_overrides(cfg, "msg-level"):
        return []
    return [f"--msg-level={_QUIET_MSG_LEVEL}"]


def _lang_defaults(cfg: Config) -> list[str]:
    """Prefer the user's languages for audio/subtitle track selection, without
    overriding any alang/slang the user already set. `--subs-with-matching-audio=no`
    means: don't force subtitles on when the audio is already in your language."""
    flags = []
    if cfg.audio_langs and not _user_overrides(cfg, "alang"):
        flags.append("--alang=" + ",".join(cfg.audio_langs))
    if cfg.subtitle_langs and not _user_overrides(cfg, "slang"):
        flags.append("--slang=" + ",".join(cfg.subtitle_langs))
        if not _user_overrides(cfg, "subs-with-matching-audio"):
            flags.append("--subs-with-matching-audio=no")
    return flags


def play(
    cfg: Config,
    title: str,
    url: str,
    *,
    start: float | None = None,
    sub_paths: tuple[str, ...] = (),
    audio_id: int | None = None,
    sub_id: int | str | None = None,
    next_label: str | None = None,
    resume_msg: str | None = None,
    work_dir: str | None = None,
) -> tuple[float, float, bool]:
    """Play `url` in mpv. Returns (position, duration, advance) where `advance`
    is True when the next-episode overlay asked to continue to the next episode.

    `work_dir` holds the IPC socket and overlay files; when omitted a private
    temp dir is created and removed here (the caller passes one to share it with
    downloaded subtitles)."""
    holder = {"position": 0.0, "duration": 0.0}
    with contextlib.ExitStack() as stack:
        if work_dir is None:
            runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
            work_dir = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="nstream-", dir=runtime)
            )
        sock_path = os.path.join(work_dir, "mpv.sock")
        signal_path = os.path.join(work_dir, "signal")
        info_path = os.path.join(work_dir, "info")
        # Defaults come before mpv_args so explicit user flags win. nstream is the
        # single source of truth for resume (via --start over IPC), so
        # --no-resume-playback stops mpv's own watch-later from doing a second,
        # conflicting seek.
        args = [
            "mpv",
            f"--force-media-title={title}",
            "--no-resume-playback",
            *_quiet_defaults(cfg),
            *_hwdec_defaults(cfg),
            *_lang_defaults(cfg),
            *cfg.mpv_args,
        ]
        if start and start > 1:
            args.append(f"--start={start:.0f}")
        args += [f"--sub-file={p}" for p in sub_paths]
        # Explicit per-play track choices win over the language-preference defaults.
        if audio_id is not None:
            args.append(f"--aid={audio_id}")
        if sub_id is not None:
            args.append(f"--sid={sub_id}")
        args.append(f"--input-ipc-server={sock_path}")

        if next_label:
            with open(info_path, "w", encoding="utf-8") as f:
                f.write(next_label + "\n")
            # --script-opts-append is non-destructive: it won't clobber a user's
            # own script-opts set for other scripts.
            lua = stack.enter_context(resources.as_file(resources.files("nstream") / "nstream.lua"))
            args += [
                f"--script={lua}",
                f"--script-opts-append=nstream-info={info_path}",
                f"--script-opts-append=nstream-signal={signal_path}",
                f"--script-opts-append=nstream-lead={cfg.autoplay_lead}",
            ]
        args.append(url)
        try:
            proc = subprocess.Popen(args)
        except FileNotFoundError:
            print("nstream: mpv non trovato", file=sys.stderr)
            return (0.0, 0.0, False)
        tracker = threading.Thread(
            target=_track_position, args=(sock_path, holder, proc, resume_msg), daemon=True
        )
        tracker.start()
        proc.wait()
        tracker.join(timeout=2.0)
        advance = False
        if next_label:
            with contextlib.suppress(OSError), open(signal_path, encoding="utf-8") as f:
                advance = f.read().strip() == "next"

    return (holder["position"], holder["duration"], advance)


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


def _pick_stream(cfg: Config, results: list[Stream], *, auto: bool) -> Stream | None:
    """Rank streams by what the hardware can actually play, then auto-pick the best
    or show an fzf menu (playable first, unsupported ones last marked ⚠)."""
    if not cfg.hw_filter:
        ranked = [(stream_label(s, quality.parse_stream(s)), s) for s in results]
        return results[0] if auto else fzf(ranked, "stream> ")

    caps = quality.detect_caps()
    playable, excluded = quality.rank_streams(
        results, caps,
        max_resolution=cfg.max_resolution,
        allow_software=cfg.allow_software,
        allow_dv5=cfg.allow_dv5,
    )  # fmt: skip
    if excluded:
        reasons = ", ".join(sorted({r.reason for r in excluded if r.reason}))
        print(
            f"nstream: {len(excluded)} stream non supportati esclusi ({reasons})", file=sys.stderr
        )
    if auto:
        if playable:
            return playable[0].stream
        print("nstream: nessuno stream supportato dall'hardware", file=sys.stderr)
        return None
    # Manual: playable first, then the excluded ones flagged so they can be forced.
    items = [(stream_label(r.stream, r.info), r.stream) for r in playable]
    items += [(f"⚠ {r.reason}  {stream_label(r.stream, r.info)}", r.stream) for r in excluded]
    return fzf(items, "stream> ")


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
) -> tuple[str | None, bool]:
    """Resolve streams for one video, play it, persist progress. Returns
    (notice, advance): `notice` is a user-facing message to surface (no streams /
    not released yet) or None on success or a cancelled stream menu; `advance` is
    True when the next-episode overlay asked to continue.

    `auto` overrides `opts.auto` for this single video: the binge loop forces it
    True from the second episode on, so use `auto` (not `opts.auto`) here."""
    results = api.streams(cfg, typ, video_id)
    if not results:
        notice = _no_streams_message(cfg, typ, video_id, title)
        print(f"nstream: {notice}", file=sys.stderr)
        return (notice, False)
    chosen = _pick_stream(cfg, results, auto=auto)
    if not chosen:
        return (None, False)

    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="nstream-", dir=runtime) as work_dir:
        audio_id: int | None = None
        sub_id: str | int | None = None
        if auto:
            # --play / binge: no pre-play menu, use the language-preference defaults.
            sub_paths = (
                pick_subtitles(cfg, typ, video_id, work_dir, mode=opts.sub_mode, lang=opts.sub_lang)
                if opts.sub_mode
                else ()
            )
        else:
            sel = choose_tracks(cfg, chosen["url"], typ, video_id, work_dir)
            if sel is None:
                return (None, False)  # backed out → return to the list
            audio_id, sub_id, sub_paths = sel
        start = _resume_position(cfg, video_id) if opts.history else None
        resume_msg = f"⏵ Ripresa da {_fmt_time(start)}" if start and start > 1 else None
        name_line = next(iter((chosen.get("name") or "").splitlines()), "")
        print(f"▶ {title} — {name_line}", file=sys.stderr)
        pos, dur, advance = play(
            cfg, title, chosen["url"],
            start=start, sub_paths=sub_paths, audio_id=audio_id, sub_id=sub_id,
            next_label=next_label, resume_msg=resume_msg, work_dir=work_dir,
        )  # fmt: skip
    # Only persist a resume we can reason about: a real duration is needed for the
    # watched/near-end logic, otherwise the entry would stick forever.
    if opts.history and on_save and pos > 0 and dur > 0:
        on_save(pos, dur)
    return (None, advance)


def _play_series(
    cfg: Config, series_id: str, name: str, eps: list[Video], start_video: Video, opts: PlayOpts
) -> str | None:
    """Play a series from `start_video`, auto-advancing through the overlay.
    Returns a notice (e.g. an episode with no streams) to surface, or None."""
    idx = next((i for i, v in enumerate(eps) if v.get("id") == start_video.get("id")), None)
    if idx is None:
        return None
    auto = opts.auto  # the first episode honours --play; binge episodes auto-pick
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
        )  # fmt: skip
        if notice:
            return notice
        if not advance or nxt is None:
            return None
        idx += 1
        auto = True
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
    items = [
        (f"S{v.get('season', 0):02d}E{v.get('episode', 0):02d}  {v.get('name', '')}", v)
        for v in eps
    ]
    # Loop the episode picker so finishing/backing out returns here, not to the list.
    header: str | None = None
    while True:
        start_video = fzf(items, "episodio> ", header=header)
        if not start_video:
            return None
        header = _play_series(cfg, meta["id"], name, eps, start_video, opts)


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


def _pick_meta(items: list[tuple[str, Meta]], cfg: Config, opts: PlayOpts) -> int:
    """Loop the title list: play a pick, then return here. ESC leaves to the caller
    (HOME or the shell). A notice from playback is shown as the fzf header next time."""
    header: str | None = None
    while True:
        meta = fzf(items, "titolo> ", header=header)
        if not meta:
            return 0
        header = play_meta(cfg, meta, opts)


def run_search(cfg: Config, query: str, opts: PlayOpts) -> int:
    metas = api.search(cfg, query)
    if not metas:
        print("nstream: nessun risultato", file=sys.stderr)
        return 1
    return _pick_meta([(meta_label(m), m) for m in metas], cfg, opts)


def run_browse(cfg: Config, cat: str, opts: PlayOpts) -> int:
    metas = api.catalog(cfg, "movie", cat) + api.catalog(cfg, "series", cat)
    if not metas:
        print("nstream: catalogo vuoto", file=sys.stderr)
        return 1
    return _pick_meta([(meta_label(m), m) for m in metas], cfg, opts)


def run_continue(cfg: Config, opts: PlayOpts) -> int:
    """`-c`: resume from history, returning to the list after each play (ESC exits)."""
    entries = state.recent(cfg)
    if not entries:
        print("nstream: cronologia vuota", file=sys.stderr)
        return 0
    header: str | None = None
    while True:
        items = [(history_label(e), e) for e in entries]
        chosen = fzf(items, "continua> ", header=header)
        if chosen is None:
            return 0
        header = play_history(cfg, chosen, opts)
        entries = state.recent(cfg)  # reflect updated positions, then re-show


# Home-menu action kinds (the value half of an fzf item; history entries are dicts).
_SEARCH = "search"
_BROWSE = "browse"
_SETTINGS = "settings"


def run_home(cfg: Config, opts: PlayOpts) -> int:
    """The TUI home: continue-watching + search + browse + settings, in one menu.
    Loops until the user backs out (ESC). This is the rich entry surface — the
    desktop/fuzzel launcher only opens it; no UI logic lives in fuzzel."""
    header: str | None = None
    while True:
        items: list[tuple[str, object]] = []
        if opts.history:
            items += [(history_label(e), e) for e in state.recent(cfg)]
        items += [
            ("🔍  Cerca…", (_SEARCH, "")),
            ("🔥  Popolari", (_BROWSE, "popolari")),
            ("🆕  Novità", (_BROWSE, "nuovi")),
            ("⭐  Top IMDb", (_BROWSE, "top")),
            ("⚙   Impostazioni", (_SETTINGS, "")),
        ]
        chosen = fzf(items, "nstream> ", header=header)
        header = None
        if chosen is None:
            return 0
        if not isinstance(chosen, tuple):  # a continue-watching entry
            header = play_history(cfg, cast(HistoryEntry, chosen), opts)
            continue
        kind, value = chosen
        if kind == _SEARCH:
            try:
                query = input("cerca> ").strip()
            except EOFError:
                return 0
            if query:
                run_search(cfg, query, opts)
        elif kind == _BROWSE:
            run_browse(cfg, CAT_MAP[cast(str, value)], opts)
        elif kind == _SETTINGS:
            settings.run_settings(cfg)
            cfg = load()  # pick up any change for the next loop


def _dispatch(cfg: Config, args: argparse.Namespace, opts: PlayOpts) -> int:
    if args.cont:
        return run_continue(cfg, opts)
    if args.browse:
        return run_browse(cfg, CAT_MAP[args.browse], opts)
    query = " ".join(args.query)
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
    parser = argparse.ArgumentParser(
        prog="nstream",
        description="Native Stremio-like client (Cinemeta + Torrentio + Real-Debrid + mpv).",
    )
    parser.add_argument("query", nargs="*", help="titolo da cercare (altrimenti chiede)")
    parser.add_argument("--play", action="store_true", help="riproduci subito il primo stream")
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
    parser.add_argument("--version", action="version", version=f"nstream {__version__}")
    args = parser.parse_args()

    try:
        cfg = _ensure_config()
    except ConfigError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return 2

    if args.settings:
        settings.run_settings(cfg)
        return 0

    sub_mode, sub_lang = _sub_options(args)
    opts = PlayOpts(
        auto=args.play,
        sub_mode=sub_mode,
        sub_lang=sub_lang,
        history=cfg.history_enabled and not args.no_history,
        autoplay=cfg.autoplay and not args.no_autoplay,
    )
    try:
        return _dispatch(cfg, args, opts)
    except api.NetworkError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return 1


def _entry() -> None:
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)


if __name__ == "__main__":
    _entry()
