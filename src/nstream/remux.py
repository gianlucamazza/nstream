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
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import bridge, caster, log, serve, ui, util
from .config import Config

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
_START_POLL = 2.0

# Minimum free disk (GB) demanded when the source size is UNKNOWN (no parsed release size):
# ffmpeg fetches the whole remote stream, so an unsized remux on a nearly-full disk can
# still fill it. With a known size the proportional `size_gb * 1.1` check applies instead.
_MIN_FREE_GB = 5.0


def needs_remux(audio_codec: str) -> bool:
    """True when `audio_codec` (a `quality.StreamInfo.audio` or a real ffprobe codec name)
    is one the Default Media Receiver can't decode, so the title needs a Tier-2 remux."""
    return (audio_codec or "").lower() in _UNDECODABLE


def available() -> bool:
    """ffmpeg present on PATH (required to remux). catt is checked by `caster`."""
    return shutil.which("ffmpeg") is not None


def _decodable(codec: str) -> bool:
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


def remux_for_cast(url: str, cfg: Config, *, audio_index: int, size_gb: float = 0.0) -> str | None:
    """Remux `url` to a complete temp MP4 keeping the audio track at `audio_index` (the cast
    decision in `stream_select.vet_cast_audio` picks it by language), and return the temp
    path — or None on failure / a refused size guard (caller then degrades to a direct cast).
    Probes once for the channel count (bitrate), video-stream count (DV7 warning) and duration
    (progress line); `size_gb` gates the disk and big-download guards. The url is passed only
    to ffprobe/ffmpeg, never logged."""
    if not cfg.cast_remux or not available():
        return None
    t, n_video, duration = _probe_meta(url)
    return remux_to_file(
        url, cfg, audio_index=audio_index, audio=t.audio,
        n_video=n_video, duration=duration, size_gb=size_gb,
    )  # fmt: skip


# --- temp file + state tracking -------------------------------------------


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = Path(base) / "nstream" / "remux"
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


def _gc_stale() -> None:
    """Best-effort: remove temp remuxes from a previous run whose serving catt is gone.
    Keeps the cache from leaking when a headless cast is never explicitly stopped."""
    st = _read_state()
    keep = st.get("file") if st and _pid_alive(st.get("pid")) else None
    with contextlib.suppress(OSError):
        for f in _cache_dir().glob("cast-*.mp4"):
            if str(f) != keep:
                f.unlink(missing_ok=True)
        for f in _cache_dir().glob("catt-*.log"):  # startup-diagnosis stderr leftovers
            f.unlink(missing_ok=True)


def _read_state() -> dict | None:
    return _runstate().read()


def _write_state(pid: int, file: str, device: str | None, mode: str = "catt") -> None:
    """Track the serving process for `--stop`/GC. `mode` is "catt" (detached catt serves+casts)
    or "serve" (our Range server serves, castbridge casts) so `stop()` tears down the right
    receiver session."""
    _runstate().write({"pid": pid, "file": file, "device": device, "mode": mode})


def _clear_state() -> None:
    _runstate().clear()


def _pid_alive(pid: int | None) -> bool:
    return util.pid_alive(pid)


def _rm(path: str | None) -> None:
    if path:
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
    """Free space (GB) on the filesystem holding `path`, or 0.0 if it can't be determined."""
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return 0.0


def _confirm(msg: str) -> bool:
    """Ask y/n on an interactive tty; headless (no tty) → proceed without blocking (the
    release was the only castable option, and a JSON/auto caller must not hang on input)."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return True
    try:
        ans = input(f"{msg} — procedo? [s/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("s", "si", "sì", "y", "yes")


def _run_ffmpeg(cmd: list[str], duration: float) -> tuple[int | None, str]:
    """Run the ffmpeg remux, rendering a percentage line from its `-progress` stream so the
    (minutes-long) prepare isn't a silent wait. Returns `(returncode, stderr)`; rc is None if
    ffmpeg couldn't be launched. Progress is best-effort — any parse hiccup just shows no bar.

    stderr goes to an unnamed temp file (read back after `wait()`), not a pipe: the progress
    loop only drains stdout, so a chatty ffmpeg (>64KB of network/demux warnings on a long
    remote remux) would fill an undrained stderr pipe and deadlock the whole remux — same
    no-reader problem as the detached catt's stderr capture below."""
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd, stdout=subprocess.PIPE, stderr=err, text=True
            )
        except (OSError, subprocess.SubprocessError) as e:
            return None, str(e)
        pct = -1
        show = duration > 0 and sys.stderr.isatty()
        if proc.stdout is not None:
            for line in proc.stdout:
                if show and line.startswith("out_time_us="):
                    try:
                        cur = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    new = min(99, int(cur / duration * 100))
                    if new != pct:
                        pct = new
                        msg = f"\r{ui.g().tv} preparo l'audio per il cast… {pct}%"
                        print(msg, end="", file=sys.stderr, flush=True)
        proc.wait()
        if pct >= 0:
            print(f"\r{ui.g().tv} audio pronto, avvio il cast.        ", file=sys.stderr)
        err.seek(0)
        stderr = err.read().decode("utf-8", errors="replace")
    return proc.returncode, stderr


def remux_to_file(
    url: str,
    cfg: Config,
    *,
    audio_index: int = 0,
    audio: list | None = None,
    n_video: int = 0,
    duration: float = 0.0,
    size_gb: float = 0.0,
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
    # the cast (best-effort), so both branches gate on a truthy `free`.
    free = _free_gb(_cache_dir())
    if size_gb > 0:
        if free and free < size_gb * 1.1:
            _log.warning(
                "spazio insufficiente per il remux: %.1fGB liberi < ~%.1fGB", free, size_gb
            )
            print(
                f"nstream: spazio disco insufficiente per il remux "
                f"(~{size_gb:.0f}GB, {free:.0f}GB liberi) — cast diretto",
                file=sys.stderr,
            )
            return None
        cap = cfg.cast_remux_max_size_gb
        if cap and size_gb > cap:
            ask = f"il cast richiede di scaricare+remuxare ~{size_gb:.0f}GB (cap {cap}GB)"
            if not _confirm(ask):
                print("nstream: remux annullato", file=sys.stderr)
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
        print(
            f"nstream: spazio disco quasi esaurito ({free:.1f}GB liberi) — cast diretto",
            file=sys.stderr,
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
    if sel is not None and _decodable(sel.codec):
        acodec = ["-c:a", "copy"]  # already DMR-decodable → keep it (no re-encode)
    else:
        codec = cfg.cast_audio_codec or "aac"
        acodec = ["-c:a", codec, "-b:a", _audio_bitrate(sel.channels if sel else None)]
    fd, path = tempfile.mkstemp(suffix=".mp4", prefix="cast-", dir=str(_cache_dir()))
    os.close(fd)
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
        "-i", url,
        "-map", "0:v:0", "-map", amap,
        "-c:v", "copy", *acodec,
        "-movflags", "+faststart", path,
    ]  # fmt: skip
    print(f"{ui.g().tv} preparo l'audio per il cast (può richiedere un po')…", file=sys.stderr)
    rc, stderr = _run_ffmpeg(cmd, duration)
    if rc != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        _log.warning("remux fallito (rc=%s): %s", rc, (stderr or "")[:300])
        _rm(path)
        return None
    return path


# --- serve + cast ---------------------------------------------------------


def cast_file(
    cfg: Config,
    title: str,
    file_path: str,
    *,
    device: str | None,
    start: float | None = None,
    sub_paths: tuple[str, ...] = (),
    follow: bool = True,
    meta: caster.CastMeta | None = None,
    on_event: caster.EventCb | None = None,
) -> tuple[float, float, bool]:
    """Cast the complete local `file_path` (a Tier-2 remux) to the DMR — the only delivery it
    accepts is a complete, Range-served file. Returns (position, duration, advance).

    Prefers the **native path** (ADR 0007): nstream's own Range HTTP server (`serve.py`) serves
    the file and **castbridge** LOADs its URL with metadata (so the TV card + HUD widget light
    up). Falls back to **catt** serving+casting (no metadata) when castbridge is unavailable or
    can't start. `follow=False` (headless) leaves the server detached and records its PID for
    `--stop`/GC; `follow=True` serves until playback ends, then removes the temp file."""
    if device:
        # The receiver fetches the file from us over the LAN, so the host firewall must let it
        # in. Best-effort + idempotent; covers both the castbridge-serve and catt-serve paths.
        serve.ensure_firewall(serve.lan_ip(device))
    if device and bridge.bridge_available():
        result = _cast_file_via_bridge(
            title, file_path, device=device, start=start,
            meta=meta or caster.CastMeta(), follow=follow, on_event=on_event,
        )  # fmt: skip
        if result is not None:
            return result
        # castbridge couldn't start → fall back to catt serving+casting below.
    base = ["catt", *(["-d", device] if device else [])]
    launch = [*base, "cast", file_path]
    if start and start > 1:
        launch += ["-t", str(int(start))]
    if sub_paths:
        launch += ["-s", sub_paths[0]]
    dest = device or "Chromecast"
    print(f"{ui.g().tv} preparo il cast su {dest}…", file=sys.stderr)
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
        print("nstream: catt non trovato", file=sys.stderr)
        _rm(file_path)
        _rm(err_path)
        return (0.0, 0.0, False)
    finally:
        os.close(err_fd)
    _write_state(proc.pid, file_path, device)

    # Confirm the receiver actually started (the serving catt has no useful exit code until
    # playback ends, so poll its status instead).
    if not _await_start(device):
        _log.warning("il cast remux non è partito entro %ss", _START_TIMEOUT)
        _log_catt_stderr(err_path)
        print("nstream: il cast non è partito", file=sys.stderr)
        if device:  # most common cause for a served file: the TV can't reach us (firewall)
            print(serve.firewall_hint(serve.lan_ip(device)), file=sys.stderr)
        _teardown(proc.pid, file_path)
        _rm(err_path)
        return (0.0, 0.0, False)
    _rm(err_path)  # startup confirmed: the capture served its (diagnosis-only) purpose

    if not follow:
        # Leave the detached catt serving; --stop / next-run GC tears it down.
        print(f"{ui.g().tv} {title} → {dest}", file=sys.stderr)
        return (0.0, 0.0, False)

    print(f"{ui.g().tv} {title} → {dest}  (Ctrl-C per smettere di seguire)", file=sys.stderr)
    try:
        proc.wait()  # catt exits when playback ends
    except KeyboardInterrupt:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run([*base, "stop"], capture_output=True, text=True)
    finally:
        _teardown(proc.pid, file_path)
    return (0.0, 0.0, False)


def _bridge_meta_kwargs(title: str, meta: caster.CastMeta, start: float | None) -> dict:
    """Metadata args for `bridge.cast_load` from a CastMeta (content type is always MP4 here)."""
    return {
        "title": title,
        "poster": meta.poster,
        "subtitle": meta.subtitle,
        "series_title": meta.series_title,
        "season": meta.season,
        "episode": meta.episode,
        "content_type": "video/mp4",
        "current_time": float(start or 0.0),
    }


def _log_bridge_failed(ev: dict) -> None:
    """Log WHY the castbridge LOAD failed before falling back to catt — the reason was
    previously discarded, making a Tier-2 startup failure undiagnosable post-mortem."""
    _log.warning(
        "castbridge non partito (%s: %s) → fallback catt",
        ev.get("error") or "?",
        ev.get("message") or "?",
    )


def _spawn_server(file_path: str, bind_ip: str) -> tuple[int, int] | None:
    """Spawn a detached `python -m nstream.serve` for `file_path` on `bind_ip`, returning
    (pid, port) once it prints its port, or None on failure. Detached so it outlives a headless
    return — the receiver streams from it for the whole runtime."""
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "nstream.serve", file_path, "--bind", bind_ip],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        _log.warning("serve detach fallito: %s", e)
        return None
    if proc.stdout is None:
        _kill(proc.pid)
        return None
    line = proc.stdout.readline().strip()
    if not line.startswith("PORT="):
        _log.warning("serve: porta non annunciata (%r)", line[:40])
        _kill(proc.pid)
        return None
    try:
        port = int(line[len("PORT=") :])
    except ValueError:
        _kill(proc.pid)
        return None
    return proc.pid, port


def _cast_file_via_bridge(
    title: str,
    file_path: str,
    *,
    device: str,
    start: float | None,
    meta: caster.CastMeta,
    follow: bool,
    on_event: caster.EventCb | None,
) -> tuple[float, float, bool] | None:
    """Serve the remux via the stdlib Range server and cast its URL with metadata via castbridge.
    Returns (pos, dur, advance) — advance is always False (a remux is a single movie) — or **None**
    when the cast never started, so `cast_file` falls back to catt (the temp file is kept).

    A Ctrl-C during the startup wait is a *user abort*, not a bridge failure: it tears down and
    re-raises so `cast_file` does NOT fall back to catt re-casting what was just cancelled."""
    bind_ip = serve.lan_ip(device)
    kwargs = _bridge_meta_kwargs(title, meta, start)

    if not follow:
        spawned = _spawn_server(file_path, bind_ip)
        if spawned is None:
            return None
        pid, port = spawned
        started = False
        for ev in bridge.cast_load(device, serve.served_url(bind_ip, port), follow=False, **kwargs):
            kind = ev.get("kind")
            if kind == "failed" and not started:
                _log_bridge_failed(ev)
                _kill(pid)  # keep the temp file for the catt fallback
                return None
            if kind == "started":
                started = True
            if on_event:
                on_event(ev)
        if not started:
            _log.warning("castbridge: LOAD senza evento started → fallback catt")
            _kill(pid)
            return None
        _write_state(pid, file_path, device, mode="serve")
        print(f"{ui.g().tv} {title} → {device}", file=sys.stderr)
        return (0.0, 0.0, False)

    # follow: in-process server (a daemon thread, dies with us); wait for playback to end.
    server, port, _thread = serve.serve_file(file_path, bind_ip)
    started = False
    disconnected = False
    pos = dur = 0.0
    try:
        for ev in bridge.cast_load(device, serve.served_url(bind_ip, port), follow=True, **kwargs):
            kind = ev.get("kind")
            if kind == "failed" and not started:
                _log_bridge_failed(ev)
                return None  # keep the temp file for the catt fallback
            if kind == "started":
                started = True
                print(
                    f"{ui.g().tv} {title} → {device}  (Ctrl-C per smettere di seguire)",
                    file=sys.stderr,
                )
            if on_event:
                on_event(ev)
            if kind in ("playing", "paused", "ended", "disconnected"):
                pos = float(ev.get("position") or pos)
                dur = float(ev.get("duration") or dur)
            if kind == "disconnected":
                # The daemon died mid-cast (socket EOF without an explicit end) — NOT a
                # playback end: the TV is still fetching from our Range server. Stop
                # following but leave the server up and the temp file on disk so playback
                # isn't cut from under the receiver; the next run's `_gc_stale` (or
                # `--stop`) reclaims the file. Honest limit: the server is an in-process
                # daemon thread, so it still dies when this nstream process exits — the
                # least-harmful option without re-architecting (vs. tearing it down NOW).
                _log.warning("daemon castbridge disconnesso a metà cast (pos=%.0fs)", pos)
                print(
                    "nstream: daemon castbridge disconnesso; la riproduzione sul TV "
                    "potrebbe interrompersi all'uscita di nstream",
                    file=sys.stderr,
                )
                disconnected = True
                break
    except KeyboardInterrupt:
        bridge.stop(device)
        if not started:
            # User abort during the startup wait — not a bridge failure: re-raise so
            # `cast_file` does NOT degrade to a detached catt re-casting the cancelled
            # file (`cli._entry` turns this into a clean exit 130). No fallback will
            # use the temp file, so it is ours to remove (finally shuts the server).
            _rm(file_path)
            raise
    finally:
        if not disconnected:
            server.shutdown()
        if started and not disconnected:
            # A started cast ran to its end → clean the temp file (fallback keeps it,
            # and a disconnect leaves it for the still-streaming receiver / GC).
            _rm(file_path)
    return (pos, dur, False) if started else None


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
        info = caster._raw_info(device)
        state = str(info.get("player_state") or "") if info else ""
        if state in ("PLAYING", "PAUSED", "BUFFERING"):
            return True
        mark = state or ("idle" if info else "unreachable")
        if not seen or seen[-1] != mark:
            seen.append(mark)
        time.sleep(_START_POLL)
    _log.warning("receiver mai in riproduzione; stati osservati: %s", ",".join(seen) or "(nessuno)")
    return False


def _teardown(pid: int | None, file_path: str | None) -> None:
    _kill(pid)
    _rm(file_path)
    _clear_state()


def stop(device: str | None = None) -> bool:
    """Stop a remux cast started by this module: stop the receiver, kill the detached
    serving catt, and remove the temp file. Best-effort; True if there was state to clear.
    Called by the CLI `--stop` path alongside `caster.stop`."""
    st = _read_state()
    if not st:
        return False
    dev = st.get("device") if device is None else device
    if st.get("mode") == "serve":
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
