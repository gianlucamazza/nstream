"""Tier-2 cast delivery: native-fidelity casting of titles whose audio the Chromecast
Default Media Receiver can't decode (AC-3/E-AC-3/DTS/TrueHD → silent on the DMR).

The DMR plays HEVC/4K/HDR video natively but only ever plays a **complete, Range-served
MP4 file** — every streaming-while-transcoding delivery (on-the-fly fMP4, HLS, a growing
file) was tested against the target TV and shows a black screen (the receiver does one
GET without a Range header and gives up). So we remux the whole stream to a complete temp
file on disk (video `-c copy` → original HEVC/4K/HDR kept, audio → AAC, the receiver
decodes it), then let **catt's own HTTP server** serve it (the one delivery the DMR
accepts). Cost: a prepare wait (the file is downloaded+remuxed before playback). The
selector prefers native-AAC releases first (`quality.score_components`), so this only
triggers for titles with no AAC alternative.

`catt cast <file>` serves the file and blocks for the whole runtime, so it is spawned in
a **detached session**: headless callers return immediately (the detached catt keeps
serving; `--stop` / a later run cleans it up), the interactive follow path waits for it to
exit (playback end). A small state file tracks the serving PID + temp path so `stop()` can
tear it down; stale temp files from a previous run are GC'd on the next remux.

Leaf module (imports `caster`/`config`/`log` + stdlib), like `engine`/`player`. The stream
url may embed a debrid token — it is passed only to ffmpeg, never logged (the `log`
redaction filter covers any leak).
"""

from __future__ import annotations

import atexit
import contextlib
import dataclasses
import fcntl
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from . import (
    bridge,
    cast_delivery,
    caster,
    languages,
    live,
    log,
    notices,
    serve,
    srt,
    subalign,
    ui,
    urlproxy,
    util,
)
from . import config as config_mod
from .config import Config
from .state import throughput as state_throughput

_log = log.get_logger("remux")

# Audio codecs the Default Media Receiver does NOT decode (passthrough-only / unsupported)
# → playback is silent unless we remux the audio to something it decodes natively.
_UNDECODABLE = frozenset({"ac3", "eac3", "dts", "dtshd", "truehd"})

# Codecs the DMR decodes natively: a first track in one of these casts directly (Tier 1),
# and a chosen track in one of these is stream-copied in a remux (no re-encode). The decision
# always comes from a real ffprobe of the chosen url — never from the release name — probed
# once and memoized per url (`tracks.probe_tracks`), so the audio guard, the cast vetting and
# the pre-remux metadata read all share a single network probe.
_DMR_DECODABLE = frozenset({"aac", "he-aac", "heaac", "mp3", "opus", "flac", "vorbis", "lpcm"})

# How long to wait for the receiver to actually start playing the served file before
# giving up (the remux already succeeded; this only confirms the cast handoff).
_START_TIMEOUT = 40.0
# Receiver-status cadence while following the detached catt (same as caster._CAST_POLL).
_STATUS_POLL = 15.0
_START_POLL = 2.0

# Minimum free disk (GB) demanded when the source size is UNKNOWN (no parsed release size):
# ffmpeg fetches the whole remote stream, so an unsized remux on a nearly-full disk can
# still fill it. With a known size the proportional `size_gb * 1.1` check applies instead.
_MIN_FREE_GB = 5.0

# Keep an advisory lock from temp creation until the detached server state is durable.
# A concurrent headless command (for example the status widget) runs `_gc_stale`; without
# this reservation it sees an in-progress remux as unowned and unlinks it under ffmpeg.
_PREPARE_LOCKS: dict[str, BinaryIO] = {}


def needs_remux(audio_codec: str) -> bool:
    """True when `audio_codec` (a `quality.StreamInfo.audio` or a real ffprobe codec name)
    is one the Default Media Receiver can't decode, so the title needs a Tier-2 remux."""
    return (audio_codec or "").lower() in _UNDECODABLE


def available() -> bool:
    """ffmpeg present on PATH (required to remux). catt is checked by `caster`."""
    return shutil.which("ffmpeg") is not None


def dmr_decodable(codec: str) -> bool:
    """True when `codec` is one the DMR plays natively (so no remux, and we can skip the probe)."""
    return (codec or "").lower() in _DMR_DECODABLE


def _probe_meta(url: str):
    """Remux metadata for `url`: embedded audio/sub tracks + video-stream count + duration,
    all read from the single memoized ffprobe in `tracks.probe_tracks` — by the time a Tier-2
    remux is decided the cast vetting already probed this url, so this is a cache hit, not a
    third network read. Returns `(Tracks, n_video, duration_s)`, all empty/zero if ffprobe is
    unavailable or the probe fails (the caller then falls back to a track-blind remux). The
    url (which may embed a debrid token) is passed only to ffprobe, never logged."""
    from . import tracks

    t = tracks.probe_tracks(url)
    return t, t.n_video, t.duration


def remux_for_cast(
    url: str,
    cfg: Config,
    *,
    audio_index: int,
    size_gb: float = 0.0,
    confirm: Callable[[str, bool], bool] | None = None,
    sub_index: int | None = None,
) -> str | None:
    """`sub_index` also extracts that embedded text subtitle to `<path>.sub.vtt` in the same
    pass (ADR 0042: the complete-file twin of the live rendition).

    Remux `url` to a complete temp MP4 keeping the audio track at `audio_index` (the cast
    decision in `cast_vet.vet_cast_audio` picks it by language), and return the temp
    path — or None on failure / a refused size guard (caller then degrades to a direct cast).
    Probes once for the channel count (bitrate), video-stream count (DV7 warning) and duration
    (progress line); `size_gb` gates the disk and big-download guards. The url is passed only
    to ffprobe/ffmpeg, never logged."""
    if not cfg.cast_remux or not available():
        return None
    t, n_video, duration = _probe_meta(url)
    return remux_to_file(
        url, cfg, audio_index=audio_index, audio=t.audio,
        n_video=n_video, duration=duration, size_gb=size_gb, confirm=confirm,
        sub_index=sub_index,
    )  # fmt: skip


def embedded_vtt(path: str) -> str:
    """Where `remux_to_file(sub_index=…)` writes the extracted subtitle track."""
    return f"{path}.sub.vtt"


def refusal(cfg: Config, size_gb: float, *, interactive: bool) -> str | None:
    """Why a Tier-2 remux of a `size_gb` release would be refused, or None if it can run.

    The decision-time twin of the guards inside `remux_to_file`: `cast_flow` asks BEFORE
    committing to a whole-file prepare, so a refusal can reselect or fail honestly instead
    of degrading to a silent direct cast (2026-10-01: 66GB remux, 52GB free → mute TV).
    Over the size cap only refuses when nobody can confirm (`interactive=False`)."""
    if not cfg.cast_remux:
        return "remux disattivato (cast_remux)"
    if not available():
        return "ffmpeg assente"
    _gc_stale()
    free = _free_gb(_cache_dir())  # 0.0 = couldn't stat → don't block (best-effort)
    if size_gb > 0:
        if free and free < size_gb * 1.1:
            return f"spazio disco insufficiente (~{size_gb:.0f}GB, {free:.0f}GB liberi)"
        cap = cfg.cast_remux_max_size_gb
        if cap and size_gb > cap and not interactive:
            return f"~{size_gb:.0f}GB oltre il limite cast_remux_max_size_gb ({cap}GB)"
    elif free and free < _MIN_FREE_GB:
        return f"spazio disco quasi esaurito ({free:.1f}GB liberi)"
    return None


# --- temp file + state tracking -------------------------------------------


def _cache_dir() -> Path:
    d = config_mod.remux_dir()
    with contextlib.suppress(OSError):
        d.mkdir(parents=True, exist_ok=True)
    return d


# State persistence + process probes live in `util.RunState`/`pid_alive`/`kill_pid`
# (shared with `mirror`); the thin module-level wrappers keep the call sites and the
# test seams (`_state_path` monkeypatching) unchanged.


def _state_path() -> Path:
    return util.RunState("remux").path


def _runstate() -> util.RunState:
    st = util.RunState("remux")
    st.path = _state_path()  # honor a repointed _state_path (tests)
    return st


def gc_stale() -> None:
    """Public, best-effort cleanup of previous-run leftovers (temp remuxes whose serving
    process is gone + stderr captures). Safe to call opportunistically — headless entry
    does, so an unattended fire-and-return that ended on its own is collected without
    waiting for the next remux."""
    _gc_stale()


def _gc_stale() -> None:
    """Best-effort: remove temp remuxes from a previous run whose serving catt is gone.
    Keeps the cache from leaking when a headless cast is never explicitly stopped."""
    st = _read_state()
    keep = st.get("file") if st and _pid_alive(st.get("pid")) else None
    with contextlib.suppress(OSError):
        for f in [*_cache_dir().glob("cast-*.mp4"), *_cache_dir().glob("cast-*.hls")]:
            path = str(f)
            if path != keep and not _prepare_active(path):
                _rm(path)
                Path(f"{path}.lock").unlink(missing_ok=True)
        # Subtitle sidecars of detached casts (`.sub.vtt`: an extracted embedded track).
        for pat in ("cast-*.mp4.srt", "cast-*.mp4.vtt", "cast-*.mp4.sub.vtt"):
            for f in _cache_dir().glob(pat):
                if not keep or not str(f).startswith(f"{keep}."):
                    f.unlink(missing_ok=True)
        for f in _cache_dir().glob("catt-*.log"):  # startup-diagnosis stderr leftovers
            f.unlink(missing_ok=True)
        for f in [*_cache_dir().glob("cast-*.mp4.lock"), *_cache_dir().glob("cast-*.hls.lock")]:
            path = str(f)[: -len(".lock")]
            if path != keep and not _prepare_active(path):
                f.unlink(missing_ok=True)
        for f in _cache_dir().glob(".cast-*.lock"):  # unpublished lock of a crashed run
            if not _prepare_active(str(f)[: -len(".lock")]):
                f.unlink(missing_ok=True)


def _read_state() -> dict | None:
    return _runstate().read()


def _write_state(
    pid: int, file: str, device: str | None, mode: str = "catt", **extra: object
) -> None:
    """Track the serving process for `--stop`/GC. `mode` is "catt" (detached catt serves+casts),
    "serve" (our Range server serves, castbridge casts) or "live" (HLS directory, ADR 0039)
    so `stop()` tears down the right receiver session. `extra`: a live cast keeps its
    playlist url and title for `live_seek`."""
    _runstate().write({"pid": pid, "file": file, "device": device, "mode": mode, **extra})
    # The durable serving state now protects the file from GC; release the preparation
    # reservation only after the state write so there is no unowned window.
    _release_prepare_lock(file)


def _clear_state() -> None:
    _runstate().clear()


def _pid_alive(pid: int | None) -> bool:
    return util.pid_alive(pid)


def _new_remux_temp(suffix: str = ".mp4") -> str:
    """Create a remux path already protected from cross-process stale-file GC: a file, or a
    directory for a live HLS cast (`suffix=".hls"`).

    The lock is created under a hidden name the GC never globs, flocked, and only THEN
    linked under its visible `cast-*.mp4.lock` name. Created visible (the old mkstemp),
    a concurrent `_gc_stale` could find it before the flock, judge it idle and unlink it —
    and later delete the remux being written, whose lock file was gone."""
    base = _cache_dir()
    fd, hidden = tempfile.mkstemp(suffix=f"{suffix}.lock", prefix=".cast-", dir=str(base))
    lock = os.fdopen(fd, "r+b")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            path = str(base / f"cast-{secrets.token_hex(4)}{suffix}")
            try:
                os.link(hidden, f"{path}.lock")  # no-clobber publish of the held lock
            except FileExistsError:
                continue
            break
        os.unlink(hidden)
        try:
            if suffix == ".hls":
                os.mkdir(path, 0o700)
            else:
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except BaseException:
            os.unlink(f"{path}.lock")
            raise
    except BaseException:
        lock.close()
        with contextlib.suppress(OSError):
            os.unlink(hidden)
        raise
    _PREPARE_LOCKS[path] = lock
    return path


def _prepare_active(path: str) -> bool:
    """Whether another process currently owns the preparation lock for `path`."""
    try:
        lock = open(f"{path}.lock", "r+b")  # noqa: SIM115
    except OSError:
        return False
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return True
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    return False


def _release_prepare_lock(path: str) -> None:
    lock = _PREPARE_LOCKS.pop(path, None)
    if lock is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    with contextlib.suppress(OSError):
        os.unlink(f"{path}.lock")


def _rm(path: str | None) -> None:
    if path:
        _release_prepare_lock(path)
        if os.path.isdir(path):  # a live HLS directory
            shutil.rmtree(path, ignore_errors=True)
            return
        with contextlib.suppress(OSError):
            os.unlink(path)


def _kill(pid: int | None) -> None:
    """Terminate the detached serving catt (its own process group)."""
    util.kill_pid(pid, pgroup=True)


# --- remux ----------------------------------------------------------------


def _audio_bitrate(channels: int | None) -> str:
    """AAC target bitrate scaled to the channel count, so a 5.1/7.1 remux isn't squeezed
    into a stereo-sized stream (the old fixed 256k under-served multichannel audio)."""
    if not channels or channels <= 2:
        return "192k"
    if channels <= 6:
        return "448k"
    return "640k"


def _free_gb(path: Path) -> float:
    """Free space (GiB, the release-size scale) on the filesystem holding `path`, or 0.0
    if it can't be determined."""
    return util.free_gib(path)


def _run_ffmpeg(cmd: list[str], duration: float, *, size_label: str = "") -> tuple[int | None, str]:
    """Run the ffmpeg remux, rendering a single in-place progress line from its
    `-progress` stream. Returns `(returncode, stderr)`; rc is None if ffmpeg couldn't
    be launched. Progress is best-effort — any parse hiccup just keeps the last frame.

    stderr goes to an unnamed temp file (read back after `wait()`), not a pipe: the progress
    loop only drains stdout, so a chatty ffmpeg (>64KB of network/demux warnings on a long
    remote remux) would fill an undrained stderr pipe and deadlock the whole remux — same
    no-reader problem as the detached catt's stderr capture below."""
    g = ui.g().tv
    size_bit = f"  {size_label}" if size_label else ""

    def _frame(pct: int | None) -> str:
        if pct is None:
            return f"{g} remux audio…{size_bit}"
        return f"{g} remux audio  {pct:3d}%{size_bit}"

    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                preexec_fn=util.die_with_parent,  # noqa: PLW1509 — awaited by this thread
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as e:
            return None, str(e)
        try:
            return _await_ffmpeg(proc, err, duration, _frame)
        except BaseException:
            # Ctrl-C / abort: stop ffmpeg BEFORE the caller deletes the partial file, or it
            # keeps writing (and downloading) into an unlinked inode.
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()
            raise


def _await_ffmpeg(
    proc: subprocess.Popen, err: BinaryIO, duration: float, _frame: Callable[[int | None], str]
) -> tuple[int | None, str]:
    """Drain `-progress` into the in-place progress line, then wait and read stderr."""
    # A tty redraws in place (every percent). A pipe still needs the wait to be
    # visible (ADR 0035): one line up front, then each 10% — `ui.progress` already
    # prints a plain newline when stderr is not a tty.
    bucket = 1 if sys.stderr.isatty() else 10
    ui.progress(_frame(0 if duration > 0 else None))
    pct = 0 if duration > 0 else -1
    if proc.stdout is not None:
        for line in proc.stdout:
            if duration > 0 and line.startswith("out_time_us="):
                try:
                    cur = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                shown = (min(99, int(cur / duration * 100)) // bucket) * bucket
                if shown != pct:
                    pct = shown
                    ui.progress(_frame(pct))
    proc.wait()
    if sys.stderr.isatty():
        # Clear the progress line; the cast "in onda" line is the next status.
        ui.progress_done()
    err.seek(0)
    stderr = err.read().decode("utf-8", errors="replace")
    return proc.returncode, stderr


@log.phase("remux")
def remux_to_file(
    url: str,
    cfg: Config,
    *,
    audio_index: int = 0,
    audio: list | None = None,
    n_video: int = 0,
    duration: float = 0.0,
    size_gb: float = 0.0,
    confirm: Callable[[str, bool], bool] | None = None,
    sub_index: int | None = None,
) -> str | None:
    """Remux `url` to a complete temp MP4 (video `-c copy`, the chosen audio track kept or
    transcoded to a DMR-decodable codec, `+faststart`) on disk. Blocks until done — this DMR
    only plays a complete file, so the whole source is fetched+remuxed before casting. Returns
    the temp path, or None on failure or a refused/over-budget guard (caller then degrades).

    `audio_index` is the audio-relative index to map with `0:a:N` (the cast decision in
    `stream_select` picks it by language; the DMR can't switch embedded tracks, so we keep
    exactly one — see Google Cast docs: the Default Media Receiver exposes only text tracks,
    audio selection needs a custom receiver). If that track is already DMR-decodable it's
    stream-copied (no re-encode / no quality loss — e.g. picking a non-default AAC track just
    drops the others); otherwise it's transcoded to `cfg.cast_audio_codec` at a channel-aware
    bitrate. `audio` (probed tracks) supplies the
    codec/channels; `n_video` ≥ 2 flags a Dolby-Vision dual-layer source (its enhancement layer
    is dropped — only `0:v:0` is mapped); `duration` feeds the progress line; `size_gb` gates
    the disk-space and big-download guards."""
    if not available():
        _log.warning("ffmpeg assente: impossibile remuxare per il cast")
        return None
    # Free-disk pre-check always runs (CLAUDE.md). `free == 0.0` is _free_gb's
    # "couldn't stat" sentinel, NOT a really-full disk: indeterminate must not block
    # the cast (best-effort), so both branches gate on a truthy `free`. Stale remuxes are
    # reaped first: a leftover file from a previous cast must not cause the refusal.
    _gc_stale()
    free = _free_gb(_cache_dir())
    if size_gb > 0:
        if free and free < size_gb * 1.1:
            _log.warning(
                "spazio insufficiente per il remux: %.1fGB liberi < ~%.1fGB", free, size_gb
            )
            notices.emit(
                f"spazio disco insufficiente per il remux "
                f"(~{size_gb:.0f}GB, {free:.0f}GB liberi) — cast diretto",
            )
            return None
        cap = cfg.cast_remux_max_size_gb
        if cap and size_gb > cap:
            # Over the cap only a person may say yes (ADR 0037): headless refuses rather
            # than starting an unattended tens-of-GB fetch.
            ask = f"il cast richiede di scaricare+remuxare ~{size_gb:.0f}GB (cap {cap}GB)"
            if not (confirm is not None and confirm(f"{ask} — procedo?", False)):
                notices.emit("remux annullato")
                return None
    elif free and free < _MIN_FREE_GB:
        # Unknown source size: no proportional check possible, but ffmpeg still fetches
        # the whole stream — demand a minimum headroom so it can't fill the disk.
        _log.warning(
            "spazio quasi esaurito (%.1fGB liberi < %.0fGB) e dimensione sconosciuta: "
            "remux rifiutato",
            free,
            _MIN_FREE_GB,
        )
        notices.emit(
            f"spazio disco quasi esaurito ({free:.1f}GB liberi) — cast diretto",
        )
        return None
    if n_video >= 2:
        _log.warning(
            "sorgente Dolby Vision dual-layer (profile 7): l'enhancement layer cade → HDR10 base"
        )
    _gc_stale()
    audio = audio or []
    # `audio_index` is the audio-relative index of the track the cast decision picked; map it
    # with ffmpeg's standard `0:a:N` (the DMR plays whatever single track we leave). `?` only on
    # the index-0 default (tolerate a video with no audio); an explicit pick stays strict so a
    # bad index errors out instead of silently producing a mute file.
    sel = audio[audio_index] if 0 <= audio_index < len(audio) else (audio[0] if audio else None)
    amap = f"0:a:{audio_index}" if audio_index > 0 else "0:a:0?"
    if sel is not None and dmr_decodable(sel.codec):
        acodec = ["-c:a", "copy"]  # already DMR-decodable → keep it (no re-encode)
    else:
        codec = cfg.cast_audio_codec or "aac"
        acodec = ["-c:a", codec, "-b:a", _audio_bitrate(sel.channels if sel else None)]
    path = _new_remux_temp()
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
        # A stalled source must fail the remux, not hang it forever (30s, microseconds).
        "-rw_timeout", "30000000", "-i", urlproxy.local_url(url),  # token kept out of argv
        "-map", "0:v:0", "-map", amap,
        "-c:v", "copy", *acodec,
        "-movflags", "+faststart", path,
        # The embedded subtitle, extracted in the same read of the source (ADR 0042).
        *(["-map", f"0:s:{sub_index}", "-c:s", "webvtt", embedded_vtt(path)]
          if sub_index is not None else []),
    ]  # fmt: skip
    size_label = f"~{size_gb:.1f}G" if size_gb > 0 else ""
    try:
        rc, stderr = _run_ffmpeg(cmd, duration, size_label=size_label)
    except KeyboardInterrupt:
        # An aborted prepare must not leak the partial multi-GB temp: the next-run GC may
        # never come for a user who just cancelled a huge fetch. ffmpeg itself dies with
        # the foreground group's SIGINT.
        ui.progress_done()
        _rm(path)
        raise
    if rc != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        _log.warning("remux fallito (rc=%s): %s", rc, (stderr or "")[:300])
        _rm(path)
        return None
    return path


# --- serve + cast ---------------------------------------------------------


@log.phase("cast_file")
def cast_file(
    cfg: Config,
    title: str,
    file_path: str,
    *,
    device: str | None,
    start: float | None = None,
    sub_paths: tuple[str, ...] = (),
    sub_lang: str | None = None,
    follow: bool = True,
    meta: caster.CastMeta | None = None,
    on_event: caster.EventCb | None = None,
) -> cast_delivery.CastResult:
    """Cast the complete local `file_path` (a Tier-2 remux) to the DMR — the only delivery it
    accepts is a complete, Range-served file. Returns a `CastResult`; the advance decision
    belongs to `cast_flow` (ADR 0029), which reads the position back, and `started` tells it
    whether the cast happened at all (ADR 0031).

    Prefers the **native path** (ADR 0007): nstream's own Range HTTP server (`serve.py`) serves
    the file and **castbridge** LOADs its URL with metadata (so the TV card + HUD widget light
    up); when `sub_paths` is set the server also serves a WebVTT track the LOAD side-loads (so subs
    now ride the native path too, `sub_lang` labelling the track). Without castbridge, the
    same server + **catt ≥0.13.2 library** `play_media_url` (title + Cinemeta/metahub
    thumb + `video/mp4` + BUFFERED in one LOAD, ADR 0050). Last resort: catt CLI
    serving+casting (`-l` + `--stream-type`; no `--thumb`).
    `follow=False` (headless) leaves the server detached and records its PID for `--stop`/GC;
    `follow=True` serves until playback ends, then removes the temp file."""
    serve.reap_sub_server()  # a new cast replaces any standalone Tier-1 subtitle server
    prev = _read_state()
    if prev and _pid_alive(prev.get("pid")):
        # The state slot is single: a new Tier-2 cast replaces the previous one. Reap the
        # old detached server first, or it would be orphaned by the overwrite below and
        # keep listening (and holding its multi-GB temp) until reboot.
        _replace_previous(prev)
    if device:
        # The receiver fetches the file from us over the LAN, so the host firewall must let it
        # in. Best-effort + idempotent; covers both the castbridge-serve and catt-serve paths.
        serve.ensure_firewall(serve.lan_ip(device))
    if device and bridge.bridge_available():
        result = _cast_file_via_bridge(
            title, file_path, device=device, start=start,
            meta=meta or caster.CastMeta(), follow=follow, on_event=on_event,
            sub_paths=sub_paths, sub_lang=sub_lang,
            app_id=(cfg.cast_receiver_app_id or "").strip(),
        )  # fmt: skip
        if result is not None:
            return result
        # castbridge couldn't start → catt library (thumb) then CLI serving+casting.
    caster.warn_catt_ignores_app_id(cfg)
    if device:
        lib = _cast_file_via_catt_lib(
            title, file_path, device=device, start=start,
            meta=meta or caster.CastMeta(), follow=follow, on_event=on_event,
            sub_paths=sub_paths, sub_lang=sub_lang,
        )  # fmt: skip
        if lib is not None:
            return lib
    if sub_paths:
        sub_paths = (caster.catt_sub(sub_paths[0]),)
    if sub_paths and not follow:
        # The subtitle lives in the caller's per-play temp dir, which is deleted as soon as
        # a headless fire-and-return returns — under a detached catt that may still have to
        # serve it. Keep a copy beside the remux (`<file_path>.vtt`), on the same
        # teardown/GC lifecycle as the mp4.
        sub_copy = f"{file_path}{os.path.splitext(sub_paths[0])[1] or '.srt'}"
        with contextlib.suppress(OSError):
            shutil.copyfile(sub_paths[0], sub_copy)
            sub_paths = (sub_copy,)
    base = ["catt", *(["-d", device] if device else [])]
    launch = caster.catt_cast_argv(
        device,
        file_path,
        title=caster.catt_display_title(title, meta),
        start=start,
        sub_path=sub_paths[0] if sub_paths else None,
    )
    dest = device or "Chromecast"
    # Capture catt's stderr to a temp file (a detached pipe would have no reader): if the
    # cast never starts, its tail says why (device unreachable, refused media, …) — the
    # diagnosis that was lost to DEVNULL. Removed once startup is confirmed (or by GC).
    err_fd, err_path = tempfile.mkstemp(suffix=".log", prefix="catt-", dir=str(_cache_dir()))
    try:
        # Detached session: catt serves the file and blocks for the whole runtime, so it
        # must outlive a headless return / not hold the foreground during follow.
        proc = subprocess.Popen(  # noqa: S603
            launch, stdout=subprocess.DEVNULL, stderr=err_fd, start_new_session=True
        )
    except (OSError, FileNotFoundError):
        notices.emit("catt non trovato")
        _rm(file_path)
        _rm(f"{file_path}.srt")
        _rm(err_path)
        return cast_delivery.CastResult(0.0, 0.0, error="catt_missing")
    finally:
        os.close(err_fd)
    _write_state(proc.pid, file_path, device)

    # Confirm the receiver actually started (the serving catt has no useful exit code until
    # playback ends, so poll its status instead).
    if not _await_start(device):
        _log.warning("il cast remux non è partito entro %ss", _START_TIMEOUT)
        _log_catt_stderr(err_path)
        notices.emit("il cast non è partito")
        if device:  # most common cause for a served file: the TV can't reach us (firewall)
            print(serve.firewall_hint(serve.lan_ip(device)), file=sys.stderr)
        _teardown(proc.pid, file_path)
        _rm(err_path)
        return cast_delivery.CastResult(0.0, 0.0, error="cast_never_started")
    _rm(err_path)  # startup confirmed: the capture served its (diagnosis-only) purpose

    # Title already shown as the play banner; live line is device-only.
    ui.cast_live(dest, follow=follow)
    if not follow:
        # Leave the detached catt serving; --stop / next-run GC tears it down. Startup was
        # confirmed just above, so this handoff really did begin (ADR 0031).
        return cast_delivery.CastResult(0.0, 0.0, bool(sub_paths), started=True)

    pos = dur = 0.0
    try:
        # catt exits when playback ends; poll the receiver meanwhile so this branch
        # honours the (position, duration, …) contract like `caster.cast` — without it
        # a Tier-2 + --follow cast would leave no resume point in the history.
        while proc.poll() is None:
            time.sleep(_STATUS_POLL)
            st = caster.status(device)
            pos = st.get("position") or pos
            dur = st.get("duration") or dur
    except KeyboardInterrupt:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run([*base, "stop"], capture_output=True, text=True)
    finally:
        _teardown(proc.pid, file_path)
    return cast_delivery.CastResult(pos, dur, bool(sub_paths), started=True)


def _bridge_meta_kwargs(
    title: str, meta: caster.CastMeta, start: float | None, *, app_id: str = ""
) -> dict:
    """Metadata args for `bridge.cast_load` from a CastMeta (content type is always MP4 here)."""
    kwargs = {
        "title": title,
        "poster": meta.poster,
        "subtitle": meta.subtitle,
        "series_title": meta.series_title,
        "season": meta.season,
        "episode": meta.episode,
        "content_type": "video/mp4",
        "current_time": float(start or 0.0),
    }
    if app_id:
        kwargs["app_id"] = app_id
    return kwargs


def _cast_file_via_catt_lib(
    title: str,
    file_path: str,
    *,
    device: str,
    start: float | None,
    meta: caster.CastMeta,
    follow: bool,
    on_event: caster.EventCb | None,
    sub_paths: tuple[str, ...] = (),
    sub_lang: str | None = None,
) -> cast_delivery.CastResult | None:
    """Serve the remux and LOAD via catt.api (title + https thumb + video/mp4 + BUFFERED).

    None when catt.api is unavailable or the LOAD fails, so `cast_file` falls back to
    the CLI (temp kept). A library timeout is confirmed on the receiver before this
    returns None — do not kill a server the TV is already reading. nstream owns the
    Range server (ADR 0007); catt is the sender only.
    """
    if not caster.catt_can_lib_load():
        return None
    bind_ip = serve.lan_ip(device)
    vtt = srt.to_vtt(sub_paths[0]) if sub_paths else None

    if not follow:
        vtt_persist: str | None = None
        if vtt:
            vtt_persist = f"{file_path}.vtt"
            try:
                shutil.copyfile(vtt, vtt_persist)
            except OSError:
                vtt_persist = None
        spawned = serve.spawn_detached(bind_ip, file_path=file_path, sub_path=vtt_persist)
        if spawned is None:
            _rm(vtt_persist)
            return None
        pid, port, token = spawned
        sub_url = serve.served_sub_url(bind_ip, port, token) if vtt_persist else ""
        ok = caster.catt_lib_play(
            device,
            serve.served_url(bind_ip, port, token),
            title=title,
            meta=meta,
            start=start,
            content_type="video/mp4",
            subtitle_url=sub_url,
        )
        if not ok or not _await_start(device):
            _kill(pid)
            _rm(vtt_persist)
            return None
        _write_state(pid, file_path, device, mode="serve")
        ui.cast_live(device, follow=False)
        if on_event:
            on_event({"kind": "started", "title": title})
        return cast_delivery.CastResult(0.0, 0.0, bool(sub_paths), started=True)

    server, port, _thread = serve.serve_file(file_path, bind_ip, sub_path=vtt)
    sub_url = serve.served_sub_url(bind_ip, port, server.token) if vtt else ""
    keep_temp = True
    try:
        ok = caster.catt_lib_play(
            device,
            serve.served_url(bind_ip, port, server.token),
            title=title,
            meta=meta,
            start=start,
            content_type="video/mp4",
            subtitle_url=sub_url,
        )
        # Do not treat a pre-start IDLE as ended — wait for PLAYING first.
        if not ok or not _await_start(device):
            return None
        keep_temp = False
        ui.cast_live(device, follow=True)
        if on_event:
            on_event({"kind": "started", "title": title})
        pos = dur = 0.0
        try:
            while True:
                time.sleep(_STATUS_POLL)
                st = caster.status(device)
                pos = st.get("position") or pos
                dur = st.get("duration") or dur
                state = str(st.get("player_state") or "")
                if state in ("IDLE", "UNKNOWN", ""):
                    break
        except KeyboardInterrupt:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(["catt", "-d", device, "stop"], capture_output=True, text=True)
        return cast_delivery.CastResult(pos, dur, bool(sub_paths), started=True)
    finally:
        server.shutdown()
        if not keep_temp:
            _rm(file_path)
            _rm(f"{file_path}.vtt")


def _cast_file_via_bridge(
    title: str,
    file_path: str,
    *,
    device: str,
    start: float | None,
    meta: caster.CastMeta,
    follow: bool,
    on_event: caster.EventCb | None,
    sub_paths: tuple[str, ...] = (),
    sub_lang: str | None = None,
    app_id: str = "",
) -> cast_delivery.CastResult | None:
    """Serve the remux via the stdlib Range server and cast its URL with metadata via castbridge.
    Returns a `CastResult`, or **None** when the cast never started, so `cast_file`
    falls back to catt (temp kept). A detached (fire-and-return) cast reports (0, 0): nobody
    polled it, and an unobserved position must never read as a finished episode (ADR 0029).

    When `sub_paths` is set, the SRT is converted to WebVTT and served as a second capability path;
    the LOAD side-loads it as an active caption track (`subs_delivered=True`). The receiver fetches
    it over the LAN, so `serve.py` sends CORS headers.

    A Ctrl-C during the startup wait is a *user abort*, not a bridge failure: it tears down and
    re-raises so `cast_file` does NOT fall back to catt re-casting what was just cancelled."""
    bind_ip = serve.lan_ip(device)
    kwargs = _bridge_meta_kwargs(title, meta, start, app_id=app_id)
    if app_id:
        notices.emit(f"ricevitore custom {app_id}")
    vtt = srt.to_vtt(sub_paths[0]) if sub_paths else None

    if not follow:
        # The vtt lives in the caller's per-play temp dir, deleted on a headless return — under a
        # detached server that still has to serve it. Keep a copy beside the remux (same
        # teardown/GC lifecycle as the mp4 and the catt-path `.srt` sidecar).
        vtt_persist: str | None = None
        if vtt:
            vtt_persist = f"{file_path}.vtt"
            try:
                shutil.copyfile(vtt, vtt_persist)
            except OSError:
                vtt_persist = None
        spawned = serve.spawn_detached(bind_ip, file_path=file_path, sub_path=vtt_persist)
        if spawned is None:
            _rm(vtt_persist)
            return None
        pid, port, token = spawned
        if vtt_persist:
            kwargs.update(serve.caption_kwargs(bind_ip, port, token, sub_lang))

        def abort(started: bool) -> bool:
            # User abort during the headless startup wait — the driver already stopped
            # the receiver session; reap the detached server (no state is written yet,
            # so `--stop` could never find it), remove the temp file (no fallback will
            # use it), and re-raise so `cast_file` does NOT degrade to a detached catt
            # re-casting what was just cancelled.
            _kill(pid)
            _rm(file_path)
            _rm(f"{file_path}.vtt")
            return True

        out = cast_delivery.drive_bridge(
            device, serve.served_url(bind_ip, port, token), follow=False,
            load_kwargs=kwargs, on_event=on_event, on_interrupt=abort,
        )  # fmt: skip
        if out is not None and out.error == "receiver_error":
            _kill(pid)  # the TV refused the media: catt would get the same refusal
            return cast_delivery.CastResult(0.0, 0.0, False, started=False, error=out.error)
        if out is None or not out.started:
            if out is not None:
                _log.warning("castbridge: LOAD senza evento started → fallback catt")
            _kill(pid)  # keep the temp file for the catt fallback
            _rm(f"{file_path}.vtt")  # the catt fallback re-derives its own sidecar
            return None
        _write_state(pid, file_path, device, mode="serve")
        ui.cast_live(device, follow=False)
        return cast_delivery.CastResult(
            0.0, 0.0, cast_delivery.caption_active(kwargs, out.tracks), started=True
        )

    # follow: in-process server (a daemon thread, dies with us); wait for playback to end.
    # The vtt lives in the caller's per-play temp dir, held open for the whole follow, so the
    # in-process server can read it live (no persist copy needed, unlike the detached path).
    server, port, _thread = serve.serve_file(file_path, bind_ip, sub_path=vtt)
    if vtt:
        kwargs.update(serve.caption_kwargs(bind_ip, port, server.token, sub_lang))

    def announce() -> None:
        ui.cast_live(device, follow=True)

    def disconnect(pos: float) -> None:
        # The daemon died mid-cast (socket EOF without an explicit end) — NOT a
        # playback end: the TV is still fetching from our Range server. Stop
        # following but leave the server up and the temp file on disk so playback
        # isn't cut from under the receiver; the next run's `_gc_stale` (or
        # `--stop`) reclaims the file. Honest limit: the server is an in-process
        # daemon thread, so it still dies when this nstream process exits — the
        # least-harmful option without re-architecting (vs. tearing it down NOW).
        _log.warning("daemon castbridge disconnesso a metà cast (pos=%.0fs)", pos)
        notices.emit(
            "daemon castbridge disconnesso; la riproduzione sul TV "
            "potrebbe interrompersi all'uscita di nstream",
        )

    def abort(started: bool) -> bool:
        # Ctrl-C during the startup wait is a user abort, not a bridge failure: the
        # temp file is ours to remove (no fallback will use it) and the re-raise stops
        # `cast_file` degrading to a detached catt re-casting what was just cancelled
        # (`cli._entry` turns it into a clean exit 130). After `started`, Ctrl-C just
        # stops following — the receiver was already stopped by the driver.
        if not started:
            _rm(file_path)
            return True
        return False

    out: cast_delivery.BridgeOutcome | None = None
    try:
        url = serve.served_url(bind_ip, port, server.token)
        out = cast_delivery.drive_bridge(
            device, url, follow=True, load_kwargs=kwargs,
            on_event=on_event, on_started=announce,
            on_disconnect=disconnect, on_interrupt=abort,
        )  # fmt: skip
    finally:
        disconnected = bool(out and out.disconnected)
        if not disconnected:
            server.shutdown()
        if out and out.started and not disconnected:
            # A started cast ran to its end → clean the temp file (fallback keeps it,
            # and a disconnect leaves it for the still-streaming receiver / GC).
            _rm(file_path)
    if out is None:
        return None  # failed before started → keep the temp file for the catt fallback
    if out.error == "receiver_error":
        return cast_delivery.CastResult(0.0, 0.0, False, started=False, error=out.error)
    if not out.started:
        return None  # never started → let the caller fall back to catt
    delivered = cast_delivery.caption_active(kwargs, out.tracks)
    return cast_delivery.CastResult(out.pos, out.dur, delivered, started=True)


# --- live HLS-TS (ADR 0039) ---------------------------------------------------

_HLS_TYPE = "application/vnd.apple.mpegurl"
# The first segment of a 4K source over a slow debrid can take a while; no growth for this
# long means the producer is stuck (or dead) and the complete-file path takes over.
_LIVE_STALL_S = 45.0
# Producers this process runs for a followed (interactive) live cast, by live dir.
_INPROC: dict[str, live.Producer] = {}
# A followed cast past this fraction of its runtime prefetches the next episode.
_NEAR_END = 0.9


def _prefetch_state() -> util.RunState:
    return util.RunState("live-prefetch")


def _comparable(job: live.Job) -> dict:
    """A job minus what legitimately differs between the prefetch and the real start: the
    resolved url (a debrid link may be minted per resolution) and the play head."""
    d = job.to_dict()
    for key in ("url", "head_s", "ss_s"):
        d.pop(key, None)
    return d


def prefetch_live(
    cfg: Config,
    url: str,
    *,
    device: str,
    audio_index: int,
    source_key: str,
    embedded: tuple[int, str] | None = None,
) -> bool:
    """Start the next episode's live producer ahead of time (binge): a detached serve with
    no LOAD. `cast_live` adopts it when the same release and job come up, so the episode
    change skips the producer start. One prefetch at a time; a stale one is reaped."""
    if not live_available(cfg) or not bridge.bridge_available():
        return False
    reap_prefetch()
    t, _n_video, _duration = _probe_meta(url)
    job = _live_job(url, cfg, t.audio, audio_index, 0.0)
    if embedded:
        job = dataclasses.replace(job, sub_map=f"0:s:{embedded[0]}", sub_lang=embedded[1])
        if cfg.sub_align and subalign.available():
            sel = t.audio[audio_index] if 0 <= audio_index < len(t.audio) else None
            job = dataclasses.replace(job, rms=True, rms_channels=(sel.channels or 0) if sel else 0)
    bind_ip = serve.lan_ip(device)
    serve.ensure_firewall(bind_ip)
    out_dir = _new_remux_temp(".hls")
    spawned = serve.spawn_detached(bind_ip, hls_dir=out_dir, job=job)
    if spawned is None:
        _rm(out_dir)
        return False
    pid, port, token = spawned
    _prefetch_state().write({
        "pid": pid, "dir": out_dir, "port": port, "token": token, "bind": bind_ip,
        "key": source_key, "job": _comparable(job),
    })  # fmt: skip
    _log.info("live: episodio successivo preparato in anticipo")
    return True


def _adopt_prefetch(job: live.Job, source_key: str, bind_ip: str) -> tuple | None:
    """(out_dir, pid, port, token) of a prefetched producer for this release and job, or
    None. Adopting it clears the prefetch slot (the cast now owns it)."""
    st = _prefetch_state().read()
    if (
        not st or not source_key or st.get("key") != source_key
        or st.get("job") != _comparable(job) or st.get("bind") != bind_ip
        or not _pid_alive(st.get("pid"))
    ):  # fmt: skip
        return None
    _prefetch_state().clear()
    _log.info("live: adotto il produttore preparato in anticipo")
    return st["dir"], int(st["pid"]), int(st["port"]), str(st["token"])


def reap_prefetch() -> None:
    """Drop a prefetched producer nobody adopted (another title, a stopped binge)."""
    st = _prefetch_state().read()
    if not st:
        return
    _kill(st.get("pid"))
    _rm(st.get("dir"))
    _prefetch_state().clear()


_LIVE_POLL_S = 0.5


# Resume points closer than this to the start are produced from 0 (the LOAD seeks there).
_LIVE_FAST_RESUME_S = 60.0


def _live_sub_input(sub_path: str, out_dir: str) -> str | None:
    """A downloaded subtitle as the live producer's second input: cleaned WebVTT (UTF-8, no
    SRT position tags) in `out_dir`, which outlives every producer generation of the cast
    and dies with it. None when it cannot be read."""
    tmp = os.path.join(out_dir, "sub_input.srt")
    try:
        shutil.copyfile(sub_path, tmp)
    except OSError:
        return None
    vtt = srt.to_vtt(tmp)
    if not vtt:
        return None
    dest = os.path.join(out_dir, "sub_input.vtt")
    try:
        os.replace(vtt, dest)
    except OSError:
        return None
    return dest


def live_available(cfg: Config) -> bool:
    """Whether a Tier-2 cast can go live (ADR 0039): config on and ffmpeg present."""
    return cfg.cast_remux and cfg.cast_live and available()


def live_refusal(size_gb: float, duration: float) -> str | None:
    """Why the live window (10 min behind + 30 min ahead of the play head) would not fit
    on disk, or None. Far below the complete file: that is the point of going live."""
    free = _free_gb(_cache_dir())
    if not free:
        return None  # unknown → don't block (best-effort, like `refusal`)
    if size_gb > 0 and duration > 0:
        window = (live.KEEP_BEHIND_S + live.AHEAD_MAX_S) / duration * size_gb
        need = min(window, size_gb) * 1.2
    else:
        need = _MIN_FREE_GB
    if free < need:
        return f"spazio disco insufficiente per il cast in diretta (~{need:.0f}GB, {free:.0f}GB)"
    return None


def live_feasible(cfg: Config, url: str, size_gb: float) -> bool:
    """Whether a live start can be attempted for `url` (decision time, ADR 0036): config,
    tools, disk window, and not a dual-layer DV source (ADR 0044). The probe is the
    memoized one the vetting already ran."""
    if not live_available(cfg) or not bridge.bridge_available():
        return False
    _t, n_video, duration = _probe_meta(url)
    if n_video >= 2:
        return False  # EL in MPEG-TS: the DMR refuses; complete-file drops 0:v:1
    return live_refusal(size_gb, duration) is None


def _live_head_ok(out_dir: str, *, gen: int = 0, subs: bool = False) -> bool:
    """Whether the first produced TS is DMR-sane (ADR 0044): a decodable video codec and
    audio with channels. Local path only — never a debrid URL."""
    from . import quality, tracks

    tl = live.timeline(out_dir, gen, subs)
    if not tl:
        return False
    path = os.path.join(out_dir, live.segment_name(gen, tl[0][0], subs))
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 1:
            return False
    except OSError:
        return False
    t = tracks.probe_tracks(path, timeout=10.0)
    if t.video_codec not in quality.CAST_VIDEO_DECODABLE:
        return False
    return bool(t.audio and (t.audio[0].channels or 0) > 0)


def _live_job(url: str, cfg: Config, audio: list, audio_index: int, head_s: float) -> live.Job:
    """The producer job: the planned track, copied when it is already DMR-decodable stereo,
    else AAC stereo (AAC 5.1 in HLS stalls this receiver — ADR 0039 matrix #7/#8)."""
    sel = audio[audio_index] if 0 <= audio_index < len(audio) else (audio[0] if audio else None)
    amap = f"0:a:{audio_index}" if audio_index > 0 else "0:a:0?"
    if sel is not None and sel.codec in ("aac", "he-aac", "heaac") and (sel.channels or 2) <= 2:
        args: tuple[str, ...] = ("-c:a", "copy")
    else:
        args = ("-c:a", cfg.cast_audio_codec or "aac", "-ac", "2", "-b:a", _audio_bitrate(2))
    return live.Job(url, amap, args, head_s=head_s)


def _await_live(
    out_dir: str,
    target_s: float,
    *,
    failed: Callable[[], bool],
    label: str,
    measure: dict | None = None,
    gen: int = 0,
    subs: bool = False,
) -> bool:
    """Wait until the playlist covers `target_s` (the resume point plus two segments).
    False when the producer fails or makes no progress for `_LIVE_STALL_S`. `measure`
    receives the production `rate` (media seconds per wall second, from the first segment
    on — ffmpeg's start-up is not the link) and `bps` (segment bytes per wall second)."""
    g = ui.g().tv
    last, last_change = -1.0, time.monotonic()
    shown = -1
    first: tuple[float, float, int] | None = None  # (wall, produced, bytes) at 1st segment
    while True:
        done = live.produced_s(out_dir, gen, subs)
        now = time.monotonic()
        if done > 0 and first is None:
            first = (now, done, _segment_bytes(out_dir))
        if done >= target_s:
            ui.progress_done()
            if measure is not None and first is not None and now - first[0] > 0.2:
                dt = now - first[0]
                measure["rate"] = (done - first[1]) / dt
                measure["bps"] = (_segment_bytes(out_dir) - first[2]) / dt
            return True
        if done > last:
            last, last_change = done, now
        if failed() or now - last_change > _LIVE_STALL_S:
            ui.progress_done()
            return False
        pct = min(99, int(done / target_s * 100)) if target_s else 0
        if pct // 10 != shown // 10:
            shown = pct
            ui.progress(f"{g} audio in diretta  {pct:3d}%{label}")
        time.sleep(_LIVE_POLL_S)


def _report_rate(url: str, measure: dict) -> None:
    """Keep the measured link throughput for the ranking (`state.throughput`) and say so
    when the source runs too close to real time to play without stalls."""
    rate, bps = measure.get("rate"), measure.get("bps")
    if bps:
        host = urllib.parse.urlsplit(url).hostname or ""
        state_throughput.record(".".join(host.split(".")[-2:]), bps)
    if rate is not None and rate < _LIVE_SLOW_RATE:
        notices.emit(
            f"la sorgente arriva a {rate:.1f}× il tempo reale: possibili interruzioni "
            "— prova --quality 1080",
            code="live_slow",
        )


def _segment_bytes(out_dir: str) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for entry in os.scandir(out_dir):
            if entry.name.endswith(".ts"):
                with contextlib.suppress(OSError):
                    total += entry.stat().st_size
    return total


# Below this production rate a live cast will stall: the link can't feed the bitrate.
_LIVE_SLOW_RATE = 1.2


@log.phase("cast_live")
def cast_live(
    cfg: Config,
    title: str,
    url: str,
    *,
    device: str,
    audio_index: int,
    start: float | None = None,
    size_gb: float = 0.0,
    sub_paths: tuple[str, ...] = (),
    sub_lang: str | None = None,
    embedded: tuple[int, str] | None = None,
    follow: bool = True,
    meta: caster.CastMeta | None = None,
    on_event: caster.EventCb | None = None,
    source_key: str = "",
    on_near_end: Callable[[], None] | None = None,
) -> cast_delivery.CastResult | None:
    """`embedded` = (subtitle index, language) of an embedded text track to deliver as an
    HLS WebVTT rendition (ADR 0042) instead of a side-loaded file. `source_key` lets a
    producer prefetched for this exact release be adopted (`prefetch_live`);
    `on_near_end` runs once, in a thread, when a followed cast passes 90 % (the binge's
    prefetch of the next episode).

    Cast `url` as a live HLS-TS playlist (ADR 0039): the TV starts as soon as the first
    segments (past the resume point) exist, instead of after the whole-file remux. Returns
    the `CastResult`, or **None** when the live tier could not start — producer failure,
    no progress, disk, or a receiver that never played it — so the caller falls back to
    the complete-file remux (`remux_for_cast` + `cast_file`). castbridge only: catt joins a
    live playlist at its edge and cannot seek it."""
    if not live_available(cfg) or not bridge.bridge_available():
        return None
    t, n_video, duration = _probe_meta(url)
    if n_video >= 2:
        _log.info("live: Dolby Vision dual-layer (EL) — complete-file remux")
        return None
    why = live_refusal(size_gb, duration)
    if why:
        _log.info("live: %s", why)
        return None
    head = float(start or 0.0)
    if duration and head >= duration - 2 * live.SEGMENT_S:
        head = 0.0
    # Fast resume: past a minute in, the producer opens the source AT the resume point
    # (seconds to start) instead of producing everything before it (223 s for 50 min,
    # field 2026-10-01). serve prepends a gap covering 0..base to the playlist
    # (`live.film_time_playlist`), so the receiver still speaks film time.
    fast = head > _LIVE_FAST_RESUME_S
    job = _live_job(url, cfg, t.audio, audio_index, 0.0 if fast else head)
    if fast:
        job = dataclasses.replace(job, ss_s=head)
    if embedded:
        job = dataclasses.replace(job, sub_map=f"0:s:{embedded[0]}", sub_lang=embedded[1])
        sub_paths = ()  # the embedded track wins over a downloaded one
    if (embedded or sub_paths) and cfg.sub_align and subalign.available():
        # Tee speech activity for the after-start alignment (ADR 0040 point 2).
        sel = t.audio[audio_index] if 0 <= audio_index < len(t.audio) else None
        job = dataclasses.replace(job, rms=True, rms_channels=(sel.channels or 0) if sel else 0)
    serve.reap_sub_server()
    prev = _read_state()
    if prev and _pid_alive(prev.get("pid")):
        _replace_previous(prev)
    bind_ip = serve.lan_ip(device)
    serve.ensure_firewall(bind_ip)
    # Adopt a prefetched producer only for a start from the top without a downloaded
    # subtitle (a prefetch carries none) — a fast resume needs a producer opened at the
    # resume point.
    adopted = _adopt_prefetch(job, source_key, bind_ip) if not (sub_paths or fast) else None
    if adopted is None:
        reap_prefetch()
    out_dir = adopted[0] if adopted else _new_remux_temp(".hls")
    # Every live subtitle is a rendition (ADR 0042): a downloaded one is the producer's
    # second input, kept in out_dir (a headless return removes the work dir it came from).
    sub_input = _live_sub_input(sub_paths[0], out_dir) if sub_paths else None
    if sub_input:
        job = dataclasses.replace(job, sub_map="1:0", sub_lang=sub_lang or "", sub_file=sub_input)
    elif sub_paths:
        _log.warning("live: sottotitolo scaricato illeggibile, cast senza sottotitoli")
        job = dataclasses.replace(job, rms=False)  # nothing to align
    producer: live.Producer | None = None
    server = None
    pid: int | None = None
    if adopted:
        _, pid, port, token = adopted  # its producer already ran ahead: the start is ~instant
    elif follow:
        producer = live.Producer.start(job, out_dir)
        if producer is None:
            _rm(out_dir)
            return None
        server, port, _thread = serve.serve_file(None, bind_ip, hls_dir=out_dir)
        server.producer = producer
        producer.run_pacing()
        live.Aligner(producer).run()
        _INPROC[out_dir] = producer
        token = server.token
    else:
        spawned = serve.spawn_detached(bind_ip, hls_dir=out_dir, job=job)
        if spawned is None:
            _rm(out_dir)
            return None
        pid, port, token = spawned

    def teardown() -> None:
        if server is not None:
            server.shutdown()
        if producer is not None:
            producer.stop()
            _INPROC.pop(out_dir, None)
        st = _read_state()
        if follow and st and st.get("file") == out_dir:  # the state this followed cast wrote
            _clear_state()
        _kill(pid)
        _rm(out_dir)

    def failed() -> bool:
        if producer is not None:
            return producer.failed()
        return not _pid_alive(pid)

    label = f"  → {int(head) // 60}:{int(head) % 60:02d}" if head > 60 else ""
    try:
        target = (0.0 if fast else head) + 2 * live.SEGMENT_S
        measure: dict = {}
        ready = _await_live(
            out_dir, target, failed=failed, label=label, measure=measure, subs=job.subs
        )
    except BaseException:
        teardown()
        raise
    if not ready:
        _log.warning(
            "live: il produttore non è partito%s",
            f" ({producer.failure_reason()})" if producer is not None else "",
        )
        teardown()
        return None
    if not _live_head_ok(out_dir, gen=0, subs=job.subs):
        _log.warning("live: primo segmento non riproducibile dal DMR — complete-file remux")
        teardown()
        return None
    _report_rate(url, measure)
    # Always the Default Media Receiver: the custom receiver (cast_receiver_app_id, ADR 0013)
    # refuses an HLS LOAD outright — LOAD_FAILED without fetching the playlist (field,
    # 2026-10-01: app CA5T0001 failed; the default app played the same url, also with a
    # start offset).
    # Film time where the playlist's first segment starts (the keyframe at or before the
    # resume point): internal playlist arithmetic only — the receiver sees film time.
    base = (live.first_pts(out_dir, 0, job.subs) or head) if fast else 0.0
    kwargs = _bridge_meta_kwargs(title, meta or caster.CastMeta(), head)
    kwargs["content_type"] = _HLS_TYPE
    if job.subs:  # castbridge activates the rendition by language (ADR 0042)
        kwargs["text_language"] = languages.bcp47(job.sub_lang or "und")  # = the master's
    load_url = serve.served_hls_url(bind_ip, port, token, live.load_name(0, job.subs))

    def abort(started: bool) -> bool:
        if not started or not follow:
            teardown()
            return True
        return False

    near_end_fired = False

    def film_time(ev: dict) -> None:
        # `--follow` JSONL speaks film time: the receiver reports playlist time (from 0
        # after a fast resume) and duration -1 for a growing playlist.
        nonlocal near_end_fired
        pos = ev.get("position")
        if (
            on_near_end is not None and not near_end_fired and duration
            and isinstance(pos, int | float) and pos >= duration * _NEAR_END
        ):  # fmt: skip
            near_end_fired = True
            threading.Thread(target=on_near_end, name="nstream-prefetch", daemon=True).start()
        if on_event is None:
            return
        ev = dict(ev)
        if "duration" in ev and not (ev.get("duration") or 0) > 0 and duration:
            ev["duration"] = round(duration, 1)
        on_event(ev)

    def started() -> None:
        ui.cast_live(device, follow=follow)
        if follow:
            # The in-process cast gets the same live state as a detached one, so the TUI's
            # seeks, sub-shift and status read it; `inproc` keeps --stop and GC from ever
            # killing this process (the owner tears down when the cast ends). An adopted
            # prefetch is a detached serve: its own pid, signalled like any other.
            inproc = producer is not None
            _write_state(
                os.getpid() if inproc else (pid or 0), out_dir, device, mode="live",
                url=load_url, title=title, base=base, duration=duration,
                subs=job.subs, text_language=kwargs.get("text_language", ""), inproc=inproc,
            )  # fmt: skip

    def disconnected(pos: float) -> None:
        _log.warning("daemon castbridge disconnesso a metà cast live (pos=%.0fs)", pos)

    out: cast_delivery.BridgeOutcome | None = None
    try:
        out = cast_delivery.drive_bridge(
            device, load_url, follow=follow,
            load_kwargs=kwargs, on_event=film_time if (on_event or on_near_end) else None,
            on_started=started, on_interrupt=abort,
            on_disconnect=disconnected if follow else None,
        )  # fmt: skip
    finally:
        # The followed cast ended: its producer and segments go with it. A daemon
        # disconnect is not an end — the TV may still be playing from our server.
        if follow and out is not None and out.started and not out.disconnected:
            teardown()
    if out is None or not out.started:
        if out is not None and out.error:
            _log.warning("live: il ricevitore ha rifiutato la playlist (%s)", out.error)
        teardown()
        return None  # → the complete-file remux takes over
    # A rendition's track id is the receiver's to assign, and castbridge activates it right
    # after the first status — which is when a fire-and-return start returns: an empty list
    # there is "not yet known" (ADR 0016), not a refusal (field 2026-10-02: [] at start,
    # [1] seconds later).
    delivered = job.subs
    if not follow:
        _write_state(
            pid or 0, out_dir, device, mode="live",
            url=load_url, title=title, base=base, duration=duration,
            subs=job.subs, text_language=kwargs.get("text_language", ""),
        )  # fmt: skip
        return cast_delivery.CastResult(0.0, 0.0, delivered, started=True)
    dur = out.dur if out.dur > 0 else duration
    return cast_delivery.CastResult(out.pos, dur, delivered, started=True)


# The receiver honours a seek on the live playlist only a little past where it plays
# (field 2026-10-01: ±30-60 s exact, +90 → +35, +180 → +5): farther, the playlist is
# LOADed again at the target, which the receiver does honour.
_LIVE_NATIVE_SEEK_S = 30.0


def live_seek(device: str | None, target: float) -> bool | None:
    """Seek the active live cast to film time `target`. None when no live cast is active —
    the caller then uses the normal media-control path. Three ways, cheapest first:

    - a short jump is the receiver's own seek (in playlist time after a fast resume);
    - a far jump inside what the producer has made — or will make within its pacing
      reach, waited for — re-LOADs the playlist there (the receiver clamps far seeks);
    - anywhere else (before the playlist's start, into pruned segments, or beyond the
      pacing reach) the producer restarts there as a new generation (`_live_restart`)."""
    st = _read_state()
    if not st or st.get("mode") != "live" or not st.get("url") or not _pid_alive(st.get("pid")):
        return None
    dev = device or st.get("device")
    if not dev or dev != st.get("device"):
        return None
    base = float(st.get("base") or 0.0)  # film time of the playlist's first segment
    gen = int(st.get("gen") or 0)
    subs = bool(st.get("subs"))
    pos = float(caster.status(dev).get("position") or 0.0)  # film time
    out_dir = str(st.get("file") or "")
    want = target - base  # in the producer's playlist time
    tl = live.timeline(out_dir, gen, subs)
    kept = [
        t for i, t, _ in tl
        if os.path.exists(os.path.join(out_dir, live.segment_name(gen, i, subs)))
    ]  # fmt: skip
    if want < 0 or (kept and want < kept[0]):
        return _live_restart(st, dev, target)
    need = want + 2 * live.SEGMENT_S
    if need > live.produced_s(out_dir, gen, subs):
        within_reach = want - (pos - base) <= live.AHEAD_MAX_S
        if not within_reach or not _await_live(
            out_dir, need, failed=lambda: False, label="", gen=gen, subs=subs
        ):
            return _live_restart(st, dev, target)
    if abs(target - pos) <= _LIVE_NATIVE_SEEK_S:
        return None  # the receiver's own seek, in film time
    return _live_load(dev, st, target)


def live_alignment(device: str | None) -> dict | None:
    """The active live cast's after-start alignment verdict (ADR 0040 point 2), or None
    when no live cast is active. An accepted offset needs no action here: serve adds it to
    every subtitle segment it serves from then on."""
    st = _read_state()
    if not st or st.get("mode") != "live" or not _pid_alive(st.get("pid")):
        return None
    dev = device or st.get("device")
    if not dev or dev != st.get("device"):
        return None
    return live.alignment(str(st.get("file") or ""))


def live_sub_shift(device: str | None, delta: float) -> float | None:
    """Move the active live cast's subtitles by `delta` seconds (cumulative; + = later).
    Only the shift file changes: serve applies the total to each subtitle segment as the
    receiver fetches it, so the cues move within its fetch-ahead (~25 s, field 2026-10-02)
    and the media is never touched. Returns the new total, or None when no live cast is
    active."""
    st = _read_state()
    if not st or st.get("mode") != "live" or not _pid_alive(st.get("pid")):
        return None
    dev = device or st.get("device")
    if not dev or dev != st.get("device"):
        return None
    path = Path(str(st.get("file") or ""), serve.SUB_SHIFT)
    try:
        total = float(path.read_text().strip() or 0.0)
    except (OSError, ValueError):
        total = 0.0
    total = round(total + delta, 3)
    try:
        util.atomic_write_bytes(path, str(total).encode(), prefix=".shift-")
    except OSError:
        return None
    return total


def _live_load(dev: str, st: dict, at: float) -> bool:
    """LOAD the live cast's current playlist at film time `at`, re-activating its subtitle
    rendition when it has one (a new LOAD starts with no text track)."""
    extra = {"text_language": st["text_language"]} if st.get("text_language") else {}
    events = bridge.cast_load(
        dev, str(st["url"]), follow=False, title=str(st.get("title") or ""),
        content_type=_HLS_TYPE, current_time=at, **extra,
    )  # fmt: skip
    try:
        for ev in events:
            if ev.get("kind") == "started":
                return True
            if ev.get("kind") == "failed":
                return False
    finally:
        events.close()
    return False


def _live_restart(st: dict, dev: str, target: float) -> bool:
    """Ask the detached serve to restart its producer at film time `target` as a new
    generation (a 0600 request file + SIGUSR1), wait for its first segments, then LOAD the
    new playlist at `target` (film time) with the new base recorded in the state."""
    out_dir = str(st.get("file") or "")
    pid = st.get("pid")
    subs = bool(st.get("subs"))
    gen = int(st.get("gen") or 0) + 1
    target = max(target, 0.0)
    if st.get("inproc"):
        producer = _INPROC.get(out_dir)
        if producer is None or int(pid or 0) != os.getpid():
            return False  # another process follows this cast: only it can restart
        producer.request_restart(target, gen)
    else:
        try:
            util.atomic_write_bytes(
                Path(out_dir, serve.RESTART_REQUEST),
                json.dumps({"ss": target, "gen": gen}).encode(),
                prefix=".restart-",
            )
            os.kill(int(pid or 0), signal.SIGUSR1)
        except (OSError, ValueError):
            return False
    if not _await_live(
        out_dir, 2 * live.SEGMENT_S, failed=lambda: not _pid_alive(pid), label="",
        gen=gen, subs=subs,
    ):  # fmt: skip
        return False
    base = live.first_pts(out_dir, gen, subs) or target
    url = f"{str(st['url']).rsplit('/', 1)[0]}/{live.load_name(gen, subs)}"
    new = {**st, "url": url, "base": base, "gen": gen}
    _runstate().write(new)
    return _live_load(dev, new, max(target, base))


def _log_catt_stderr(path: str) -> None:
    """Log the tail of the detached catt's captured stderr — why the cast never started.
    The file holds a local path (no debrid token), so the tail is safe to log."""
    with contextlib.suppress(OSError):
        tail = Path(path).read_text(errors="replace")[-500:].strip()
        if tail:
            _log.warning("catt stderr: %s", tail)


def _await_start(device: str | None) -> bool:
    """Poll the receiver until it reports playback (or buffering), or time out. On timeout,
    log the states observed so the failure mode is reconstructable: a receiver that never
    answered ("unreachable") points at the network, one that stayed IDLE answered but never
    fetched/played our file (firewall on the serve port, or refused media)."""
    deadline = time.monotonic() + _START_TIMEOUT
    seen: list[str] = []
    while time.monotonic() < deadline:
        info = caster.receiver_info(device)
        state = str(info.get("player_state") or "") if info else ""
        if state in ("PLAYING", "PAUSED", "BUFFERING"):
            return True
        mark = state or ("idle" if info else "unreachable")
        if not seen or seen[-1] != mark:
            seen.append(mark)
        time.sleep(_START_POLL)
    _log.warning("receiver mai in riproduzione; stati osservati: %s", ",".join(seen) or "(nessuno)")
    return False


def _replace_previous(prev: dict) -> None:
    """A new cast takes the single state slot: tear the previous one down — except a live
    cast another (interactive) process follows: never signal that process, it tears its
    own cast down once the receiver moves on."""
    if prev.get("inproc"):
        _clear_state()
        return
    _teardown(prev.get("pid"), prev.get("file"))


def _teardown(pid: int | None, file_path: str | None) -> None:
    _kill(pid)
    _rm(file_path)
    if file_path:
        _rm(f"{file_path}.srt")  # subtitle sidecar of the detached catt path, if any
        _rm(f"{file_path}.vtt")  # WebVTT sidecar of the detached castbridge path, if any
        _rm(embedded_vtt(file_path))  # an embedded track extracted by the remux
    _clear_state()


def stop(device: str | None = None) -> bool:
    """Stop a remux cast started by this module: stop the receiver, kill the detached
    serving catt, and remove the temp file. Best-effort; True if there was state to clear.
    Called by the CLI `--stop` path alongside `caster.stop`."""
    # A Tier-1 direct cast leaves only a standalone subtitle server (no remux state) — reap it
    # here so `--stop` tears it down too, even when there's no remux temp file to clear.
    reaped_sub = serve.reap_sub_server()
    reap_prefetch()  # a binge's prepared next episode goes with the cast
    st = _read_state()
    if not st:
        return reaped_sub
    dev = st.get("device") if device is None else device
    if st.get("inproc"):
        # A followed live cast: stopping the receiver ends it, and its owner tears down.
        bridge.stop(dev)
        return True
    if st.get("mode") in ("serve", "live"):
        # Native path: castbridge owns the receiver session; our process is the file server.
        bridge.stop(dev)
    else:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["catt", *(["-d", dev] if dev else []), "stop"], capture_output=True, text=True
            )
    _teardown(st.get("pid"), st.get("file"))
    return True


def _cleanup() -> None:
    """atexit best-effort: only used by the interactive follow path (headless deliberately
    leaves the detached server running)."""
    st = _read_state()
    if st and _pid_alive(st.get("pid")):
        return  # a detached headless server is still serving — leave it for --stop
    _clear_state()


atexit.register(_cleanup)
