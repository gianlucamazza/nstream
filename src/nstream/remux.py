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
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import bridge, caster, log, serve
from .config import Config

_log = log.get_logger("remux")

# Audio codecs the Default Media Receiver does NOT decode (passthrough-only / unsupported)
# → playback is silent unless we remux the audio to something it decodes natively.
_UNDECODABLE = frozenset({"ac3", "eac3", "dts", "dtshd", "truehd"})

# Codecs the DMR decodes natively. When a release name *positively* advertises one of these
# we trust it and skip the pre-cast ffprobe (the common Tier-1 path stays instant); the rare
# AAC-mislabelled-Dolby release then casts silent until the user notices — an accepted trade
# for not probing every cast. We still probe when the hint is empty or names an undecodable
# codec (a costly remux shouldn't fire on a misparsed name).
_DMR_DECODABLE = frozenset({"aac", "he-aac", "heaac", "mp3", "opus", "flac", "vorbis", "lpcm"})

# How long to wait for the receiver to actually start playing the served file before
# giving up (the remux already succeeded; this only confirms the cast handoff).
_START_TIMEOUT = 40.0
_START_POLL = 2.0


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
    """One ffprobe over `url`: embedded audio/sub tracks + video-stream count + duration.
    Returns `(Tracks, n_video, duration_s)`, all empty/zero if ffprobe is unavailable or the
    probe fails (the caller then falls back to the name hint / a track-blind remux). The url
    (which may embed a debrid token) is passed only to ffprobe, never logged."""
    from . import tracks, util

    cmd = [
        "ffprobe", "-v", "error", "-of", "json", "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,channels:stream_tags=language,title",
        url,
    ]  # fmt: skip
    proc = util.run_cmd(cmd, timeout=util.FFPROBE_TIMEOUT)
    if proc is None:
        return tracks.Tracks(), 0, 0.0
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return tracks.Tracks(), 0, 0.0
    data = data if isinstance(data, dict) else {}
    t = tracks._parse_ffprobe(data)
    n_video = sum(1 for s in data.get("streams", []) if s.get("codec_type") == "video")
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return t, n_video, duration


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


def _state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / "nstream-remux.json"


def _gc_stale() -> None:
    """Best-effort: remove temp remuxes from a previous run whose serving catt is gone.
    Keeps the cache from leaking when a headless cast is never explicitly stopped."""
    st = _read_state()
    keep = st.get("file") if st and _pid_alive(st.get("pid")) else None
    with contextlib.suppress(OSError):
        for f in _cache_dir().glob("cast-*.mp4"):
            if str(f) != keep:
                f.unlink(missing_ok=True)


def _read_state() -> dict | None:
    with contextlib.suppress(OSError, json.JSONDecodeError):
        return json.loads(_state_path().read_text())
    return None


def _write_state(pid: int, file: str, device: str | None, mode: str = "catt") -> None:
    """Track the serving process for `--stop`/GC. `mode` is "catt" (detached catt serves+casts)
    or "serve" (our Range server serves, castbridge casts) so `stop()` tears down the right
    receiver session."""
    with contextlib.suppress(OSError):
        _state_path().write_text(
            json.dumps({"pid": pid, "file": file, "device": device, "mode": mode})
        )


def _clear_state() -> None:
    with contextlib.suppress(OSError):
        _state_path().unlink(missing_ok=True)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _rm(path: str | None) -> None:
    if path:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _kill(pid: int | None) -> None:
    """Terminate the detached serving catt (its own process group)."""
    if not pid:
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    with contextlib.suppress(OSError, ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


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
    ffmpeg couldn't be launched. Progress is best-effort — any parse hiccup just shows no bar."""
    try:
        proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
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
                    msg = f"\r📺 preparo l'audio per il cast… {pct}%"
                    print(msg, end="", file=sys.stderr, flush=True)
    proc.wait()
    if pct >= 0:
        print("\r📺 audio pronto, avvio il cast.        ", file=sys.stderr)
    stderr = proc.stderr.read() if proc.stderr is not None else ""
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
    if size_gb > 0:
        free = _free_gb(_cache_dir())
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
    print("📺 preparo l'audio per il cast (può richiedere un po')…", file=sys.stderr)
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
    print(f"📺 preparo il cast su {dest}…", file=sys.stderr)
    try:
        # Detached session: catt serves the file and blocks for the whole runtime, so it
        # must outlive a headless return / not hold the foreground during follow.
        proc = subprocess.Popen(  # noqa: S603
            launch, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except (OSError, FileNotFoundError):
        print("nstream: catt non trovato", file=sys.stderr)
        _rm(file_path)
        return (0.0, 0.0, False)
    _write_state(proc.pid, file_path, device)

    # Confirm the receiver actually started (the serving catt has no useful exit code until
    # playback ends, so poll its status instead).
    if not _await_start(device):
        _log.warning("il cast remux non è partito entro %ss", _START_TIMEOUT)
        print("nstream: il cast non è partito", file=sys.stderr)
        if device:  # most common cause for a served file: the TV can't reach us (firewall)
            print(serve.firewall_hint(serve.lan_ip(device)), file=sys.stderr)
        _teardown(proc.pid, file_path)
        return (0.0, 0.0, False)

    if not follow:
        # Leave the detached catt serving; --stop / next-run GC tears it down.
        print(f"📺 {title} → {dest}", file=sys.stderr)
        return (0.0, 0.0, False)

    print(f"📺 {title} → {dest}  (Ctrl-C per smettere di seguire)", file=sys.stderr)
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
    when the cast never started, so `cast_file` falls back to catt (the temp file is kept)."""
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
                _kill(pid)  # keep the temp file for the catt fallback
                return None
            if kind == "started":
                started = True
            if on_event:
                on_event(ev)
        if not started:
            _kill(pid)
            return None
        _write_state(pid, file_path, device, mode="serve")
        print(f"📺 {title} → {device}", file=sys.stderr)
        return (0.0, 0.0, False)

    # follow: in-process server (a daemon thread, dies with us); wait for playback to end.
    server, port, _thread = serve.serve_file(file_path, bind_ip)
    started = False
    pos = dur = 0.0
    try:
        for ev in bridge.cast_load(device, serve.served_url(bind_ip, port), follow=True, **kwargs):
            kind = ev.get("kind")
            if kind == "failed" and not started:
                return None  # keep the temp file for the catt fallback
            if kind == "started":
                started = True
                print(f"📺 {title} → {device}  (Ctrl-C per smettere di seguire)", file=sys.stderr)
            if on_event:
                on_event(ev)
            if kind in ("playing", "paused", "ended"):
                pos = float(ev.get("position") or pos)
                dur = float(ev.get("duration") or dur)
    except KeyboardInterrupt:
        bridge.stop(device)
    finally:
        server.shutdown()
        if started:  # a started cast ran to here → clean the temp file (fallback keeps it)
            _rm(file_path)
    return (pos, dur, False) if started else None


def _await_start(device: str | None) -> bool:
    """Poll the receiver until it reports playback (or buffering), or time out."""
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        state = caster.status(device).get("player_state") or ""
        if state in ("PLAYING", "PAUSED", "BUFFERING"):
            return True
        time.sleep(_START_POLL)
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
