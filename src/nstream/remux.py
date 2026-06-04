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

from . import caster, log
from .config import Config

_log = log.get_logger("remux")

# Audio codecs the Default Media Receiver does NOT decode (passthrough-only / unsupported)
# → playback is silent unless we remux the audio to something it decodes natively.
_UNDECODABLE = frozenset({"ac3", "eac3", "dts", "dtshd", "truehd"})

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


def _probe_audio(url: str) -> str:
    """The real headline-audio codec of `url` via ffprobe (truth, vs the release-name
    heuristic — releases mistag), or "" if unprobeable. Imported lazily so a missing
    ffprobe degrades to the caller's name hint instead of failing the cast."""
    from . import tracks

    t = tracks.probe_tracks(url)
    return t.audio[0].codec.lower() if t.audio else ""


def should_remux(url: str, cfg: Config, *, hint: str = "") -> bool:
    """Whether casting `url` needs a Tier-2 audio remux: ffprobe the real audio codec (fall
    back to the release-name `hint` when ffprobe is unavailable) and check it against the
    set the Default Media Receiver can't decode. False unless `cfg.cast_remux` and ffmpeg."""
    if not cfg.cast_remux or not available():
        return False
    real = _probe_audio(url)
    return needs_remux(real or hint)


def prepare_for_cast(url: str, cfg: Config, *, hint: str = "") -> str | None:
    """If `url`'s audio needs remuxing for the DMR, remux to a complete temp file and
    return its path; else None (the caller casts `url` directly). The url is passed only to
    ffprobe/ffmpeg, never logged."""
    if not should_remux(url, cfg, hint=hint):
        return None
    return remux_to_file(url, cfg)


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


def _write_state(pid: int, file: str, device: str | None) -> None:
    with contextlib.suppress(OSError):
        _state_path().write_text(json.dumps({"pid": pid, "file": file, "device": device}))


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


def remux_to_file(url: str, cfg: Config) -> str | None:
    """Remux `url` to a complete temp MP4 (video `-c copy`, audio → `cfg.cast_audio_codec`,
    `+faststart`) on disk. Blocks until done — this DMR only plays a complete file, so the
    whole source is fetched+remuxed before casting. Returns the temp path, or None on
    failure (caller then degrades, e.g. to the H.264 mirror or a direct cast)."""
    if not available():
        _log.warning("ffmpeg assente: impossibile remuxare per il cast")
        return None
    _gc_stale()
    codec = cfg.cast_audio_codec or "aac"
    fd, path = tempfile.mkstemp(suffix=".mp4", prefix="cast-", dir=str(_cache_dir()))
    os.close(fd)
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        url,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
        "-c:a",
        codec,
        "-b:a",
        "256k",
        "-movflags",
        "+faststart",
        path,
    ]
    print("📺 preparo l'audio per il cast (può richiedere un po')…", file=sys.stderr)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as e:
        _log.warning("remux fallito: %s", e)
        _rm(path)
        return None
    if proc.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        _log.warning("remux fallito (rc=%s): %s", proc.returncode, (proc.stderr or "")[:300])
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
) -> tuple[float, float, bool]:
    """Cast the complete local `file_path` by letting catt serve it (the only delivery the
    DMR accepts). Returns (position, duration, advance) like `caster.cast`.

    `follow=False` (headless): spawn a detached `catt cast`, confirm the receiver started,
    return — the detached catt keeps serving; `stop()` (or the next run's GC) cleans up the
    server + temp file. `follow=True`: wait for the detached catt to exit (playback end),
    then remove the temp file."""
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
