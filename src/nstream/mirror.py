"""Realtime cast via the native Cast Streaming mirror sender.

An alternative cast backend to the Default-Media-Receiver path (`caster`/`remux`):
instead of handing the receiver a file, mpv decodes the stream locally and a custom
openscreen sender mirrors it to the TV over RTP (hardware H.264, ~120ms). It starts
instantly — no download/remux — at the cost of 1080p SDR (no 4K/HDR/Dolby passthrough).

The correct, source-clean topology (validated live):
  - mpv plays on a **headless virtual output** (`hyprctl output create headless`) — never
    shown on a physical monitor, always composited, full 1080p. No visible window, no
    occlusion, no screen real estate taken.
  - the sender captures that mpv window **by Hyprland address** (`window:addr=`), the
    reliable capture path (the sender's capture-by-output-name selects the wrong output).
  - audio is routed to a dedicated **null sink** and captured from its monitor passively
    (`--audio-sink`): no per-app graph coupling (which corrupts the source + desyncs), and
    the laptop stays silent (the movie plays only on the TV).
  - `--playout-delay 500`: a movie isn't interactive, so the tight 120ms mirror jitter
    buffer (which starves the audio in-flight budget into constant drops at ~110ms RTT) is
    replaced by a roomy one.

Leaf module below `cli` (imports `player`/`config`/`log`/`ui`/`util` + stdlib), like `remux`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import cast_delivery, log, player, ui, util
from .config import Config

_log = log.get_logger(__name__)

# Fixed names for the per-cast PipeWire null sink and its mpv audio device.
_SINK_NAME = "nstream_cast"
_MPV_AUDIO_DEVICE = f"pipewire/{_SINK_NAME}"
# A unique-ish mpv window title so we can find our own window if the PID lookup races.
_MPV_TITLE = "nstream-mirror"
_DEFAULT_BITRATE = 16_000_000
_DEFAULT_PLAYOUT_MS = 500
_CAST_PORT = 8009
# How long to wait for mpv's window to appear in the Hyprland tree.
_WINDOW_POLL_RETRIES = 40
_WINDOW_POLL_DELAY = 0.1


def _sender_bin() -> str:
    """The openscreen Cast Streaming sender binary (not bundled — built separately)."""
    env = os.environ.get("CAST_MIRROR_BIN")
    if env:
        return env
    return os.path.expanduser(
        "~/Workspace/tooling/openscreen-build/openscreen/out/Default/cast_sender"
    )


def unavailable_reason() -> str | None:
    """Why the mirror backend can't run here, or None when it can. The headless output
    and window placement are Hyprland IPC (`hyprctl`), so under another compositor the
    mirror is off — said out loud, because it silently disables the ADR 0015/0022
    fallbacks a huge or .mkv remux relies on."""
    bin_path = _sender_bin()
    if not (os.path.isfile(bin_path) and os.access(bin_path, os.X_OK)):
        return "cast_sender non installato"
    if not shutil.which("hyprctl"):
        return "richiede Hyprland (hyprctl)"
    if not shutil.which("pactl"):
        return "pactl assente"
    return None


def available() -> bool:
    """True when the mirror backend can run (see `unavailable_reason`). A clean guard so the
    caller can degrade to the DMR path with a clear message instead of failing mid-cast."""
    return unavailable_reason() is None


# --- low-level helpers ----------------------------------------------------


def _hypr(*args: str) -> str:
    """Run `hyprctl <args>`; return stdout (empty on failure). Best-effort."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        proc = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=5)
        return proc.stdout
    return ""


def _hypr_json(*args: str) -> Any:
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(_hypr(*args, "-j") or "null")
    return None


def _headless_names() -> set[str]:
    mons = _hypr_json("monitors")
    if not isinstance(mons, list):
        return set()
    return {
        str(m["name"])
        for m in mons
        if isinstance(m, dict) and str(m.get("name", "")).startswith("HEADLESS")
    }


def _create_headless() -> str | None:
    """Create a headless virtual output and return its new name (HEADLESS-N), or None."""
    before = _headless_names()
    _hypr("output", "create", "headless")
    # The name appears asynchronously; poll briefly for the new one.
    for _ in range(20):
        new = _headless_names() - before
        if new:
            return sorted(new)[0]
        time.sleep(0.1)
    return None


def _headless_workspace(name: str) -> int | None:
    mons = _hypr_json("monitors")
    if isinstance(mons, list):
        for m in mons:
            if isinstance(m, dict) and m.get("name") == name:
                aws = m.get("activeWorkspace")
                ws = aws.get("id") if isinstance(aws, dict) else None
                return int(ws) if ws is not None else None
    return None


def _window_addr(pid: int) -> str | None:
    """Resolve the Hyprland address of the window owned by `pid`."""
    clients = _hypr_json("clients")
    if isinstance(clients, list):
        for c in clients:
            if isinstance(c, dict) and c.get("pid") == pid:
                addr = c.get("address")
                return str(addr) if addr else None
    return None


def _load_null_sink() -> int | None:
    """Create the dedicated null sink; return its module id for later unload."""
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        proc = subprocess.run(
            [
                "pactl", "load-module", "module-null-sink",
                f"sink_name={_SINK_NAME}",
                "sink_properties=node.description=nstream-cast",
            ],
            capture_output=True, text=True, timeout=5,
        )  # fmt: skip
        if proc.returncode == 0 and proc.stdout.strip():
            return int(proc.stdout.strip())
    return None


def _unload_module(module_id: int | None) -> None:
    if module_id is None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["pactl", "unload-module", str(module_id)],
            capture_output=True, text=True, timeout=5,
        )  # fmt: skip


def _pid_alive(pid: int | None) -> bool:
    return util.pid_alive(pid)


def _kill(pid: int | None) -> None:
    # No pgroup here: mpv and the sender are signalled individually (unlike remux's
    # detached catt, which owns a whole serving process group).
    util.kill_pid(pid)


# --- state tracking (for headless --stop) ---------------------------------

# Persistence shared with `remux` via `util.RunState`; thin wrappers keep the call
# sites and the test seams (`_state_path` monkeypatching) unchanged.


def _state_path() -> Path:
    return util.RunState("mirror").path


def _runstate() -> util.RunState:
    st = util.RunState("mirror")
    st.path = _state_path()  # honor a repointed _state_path (tests)
    return st


def _write_state(state: dict) -> None:
    _runstate().write(state)


def _read_state() -> dict | None:
    return _runstate().read()


def _clear_state() -> None:
    _runstate().clear()


def _teardown(state: dict) -> None:
    """Tear down everything a mirror cast created, in dependency order. Idempotent."""
    _kill(state.get("sender_pid"))
    _kill(state.get("mpv_pid"))
    time.sleep(0.3)
    headless = state.get("headless")
    if headless:
        _hypr("output", "remove", headless)
    _unload_module(state.get("sink_module"))
    work_dir = state.get("work_dir")
    if work_dir:
        # Per-cast work dir (mpv IPC socket) under $XDG_RUNTIME_DIR: without this it
        # leaks until logout on the detached path. Best-effort, never blocks teardown.
        shutil.rmtree(work_dir, ignore_errors=True)
    _clear_state()


def stop() -> bool:
    """Tear down a mirror cast started by this module. Called by the CLI `--stop` path.
    Best-effort; True if there was state to clear."""
    st = _read_state()
    if not st:
        return False
    _teardown(st)
    return True


# --- the cast ------------------------------------------------------------


# Tone-map HDR→SDR for the mirror (ADR 0023). mpv renders onto an 8-bit SDR Wayland surface
# that the H.264 Cast Streaming sender captures, so an HDR (BT.2020/PQ) source rendered with no
# tone-map arrives clipped near-black on the TV (audio, on the independent null sink, plays fine
# — the exact symptom seen on a 4K HDR cast). Pinning an SDR (BT.709/1886) target makes mpv's GPU
# renderer tone-map to the captured surface. A no-op for SDR sources. Injected before
# `cfg.mpv_args`, so a user override still wins.
_TONEMAP_ARGS = (
    "--target-prim=bt.709",
    "--target-trc=bt.1886",
    "--tone-mapping=bt.2390",
)


def _mpv_args(
    cfg: Config,
    url: str,
    sock_path: str,
    *,
    start: float | None,
    sub_paths: tuple[str, ...],
    audio_id: int | None,
    sub_id: int | str | None,
) -> list[str]:
    """Build the mpv command: same defaults as `player.play`, plus headless-mirror
    specifics (route audio to the null sink, fullscreen, a known title, IPC socket)."""
    args = [
        "mpv",
        f"--title={_MPV_TITLE}",
        "--no-resume-playback",
        "--fullscreen",
        "--force-window=yes",
        f"--audio-device={_MPV_AUDIO_DEVICE}",
        # Small audio buffer: keeps the null-sink monitor delivery steady, avoiding the
        # ~200ms re-anchor gaps mpv's default 200ms buffer causes.
        "--audio-buffer=0.05",
        *player._quiet_defaults(cfg),
        *player._hwdec_defaults(cfg),
        *player._lang_defaults(cfg),
        *player._stream_cache_defaults(cfg),
        *_TONEMAP_ARGS,
        *cfg.mpv_args,
    ]
    if start and start > 1:
        args.append(f"--start={start:.0f}")
    args += [f"--sub-file={p}" for p in sub_paths]
    if audio_id is not None:
        args.append(f"--aid={audio_id}")
    if sub_id is not None:
        args.append(f"--sid={sub_id}")
    args.append(f"--input-ipc-server={sock_path}")
    args.append(url)
    return args


def cast_via_mirror(
    cfg: Config,
    title: str,
    url: str,
    *,
    device: str,
    start: float | None = None,
    sub_paths: tuple[str, ...] = (),
    audio_id: int | None = None,
    sub_id: int | str | None = None,
    follow: bool = True,
) -> cast_delivery.CastResult:
    """Cast `url` to `device` (a TV IP) by mirroring a headless mpv. Returns a `CastResult`
    like `caster.cast` — `cast_flow` derives the advance (ADR 0029) and reads `started` to
    decide whether the cast happened at all (ADR 0031).

    `follow=True` (interactive): block until mpv exits, tracking position over IPC, then
    tear everything down. `follow=False` (headless): leave mpv + sender detached and return;
    `stop()` (CLI `--stop`) tears them down."""
    if not available():
        print(
            "nstream: sender mirror non disponibile (build openscreen / $CAST_MIRROR_BIN)",
            file=sys.stderr,
        )
        return cast_delivery.CastResult(0.0, 0.0, error="mirror_unavailable")

    # A previous mirror left running? Clear it first (single active mirror).
    old = _read_state()
    if old:
        _teardown(old)

    runtime = str(util.runtime_dir())
    work_dir = tempfile.mkdtemp(prefix="nstream-mirror-", dir=runtime)
    sock_path = os.path.join(work_dir, "mpv.sock")
    # The work dir travels in the state so `stop()` (detached path) and `_teardown`
    # (follow / failure paths) can remove it — it would otherwise leak until logout.
    state: dict = {"device": device, "work_dir": work_dir}

    try:
        state["sink_module"] = _load_null_sink()
        headless = _create_headless()
        if not headless:
            print("nstream: impossibile creare l'output headless", file=sys.stderr)
            _teardown(state)
            return cast_delivery.CastResult(0.0, 0.0, error="headless_output_failed")
        state["headless"] = headless

        args = _mpv_args(
            cfg, url, sock_path,
            start=start, sub_paths=sub_paths, audio_id=audio_id, sub_id=sub_id,
        )  # fmt: skip
        # Headless cast must outlive nstream's exit → detached session.
        try:
            proc = subprocess.Popen(args, start_new_session=not follow)
        except OSError:
            # Not just FileNotFoundError: a PermissionError/other OSError from Popen
            # must also unwind the already-mounted sink + headless output.
            print("nstream: mpv non trovato", file=sys.stderr)
            _teardown(state)
            return cast_delivery.CastResult(0.0, 0.0, error="mpv_missing")
        state["mpv_pid"] = proc.pid

        addr = _await_window(proc.pid)
        if not addr:
            print("nstream: la finestra mpv non è comparsa", file=sys.stderr)
            _kill(proc.pid)
            _teardown(state)
            return cast_delivery.CastResult(0.0, 0.0, error="mpv_window_missing")

        # Move mpv onto the headless output (off the user's monitors). mpv is already
        # fullscreen (`--fullscreen`), and that state follows the window across the silent
        # move — so we must NOT call `dispatch fullscreen` (it has no address arg and would
        # toggle whatever window is *focused*, e.g. the user's terminal).
        ws = _headless_workspace(headless)
        if ws is not None:
            _hypr("dispatch", "movetoworkspacesilent", f"{ws},address:{addr}")
            time.sleep(0.2)
        addr = _window_addr(proc.pid) or addr  # re-resolve (stable after the move)

        sender_pid = _launch_sender(cfg, device, addr)
        if sender_pid is None:
            print("nstream: avvio sender mirror fallito", file=sys.stderr)
            _kill(proc.pid)
            _teardown(state)
            return cast_delivery.CastResult(0.0, 0.0, error="sender_launch_failed")
        state["sender_pid"] = sender_pid
        _write_state(state)

        print(f"{ui.g().tv} {title} → {device} (mirror 1080p)", file=sys.stderr)
        if not follow:
            # Detached mirror: mpv is up and the sender is streaming to the TV — started.
            return cast_delivery.CastResult(0.0, 0.0, started=True)

        # Interactive: track position over IPC and block until mpv exits.
        holder = {"position": 0.0, "duration": 0.0}
        tracker = threading.Thread(
            target=player._track_position, args=(sock_path, holder, proc), daemon=True
        )
        tracker.start()
        try:
            proc.wait()
        except KeyboardInterrupt:
            _kill(proc.pid)
        tracker.join(timeout=player._TRACKER_JOIN_TIMEOUT)
        return cast_delivery.CastResult(holder["position"], holder["duration"], started=True)
    finally:
        if follow:
            _teardown(_read_state() or state)
            with contextlib.suppress(OSError):
                shutil.rmtree(work_dir, ignore_errors=True)


def _await_window(pid: int) -> str | None:
    for _ in range(_WINDOW_POLL_RETRIES):
        if not _pid_alive(pid):
            return None
        addr = _window_addr(pid)
        if addr:
            return addr
        time.sleep(_WINDOW_POLL_DELAY)
    return None


def _launch_sender(cfg: Config, device: str, addr: str) -> int | None:
    bitrate = getattr(cfg, "mirror_bitrate", 0) or _DEFAULT_BITRATE
    playout = getattr(cfg, "mirror_playout_ms", 0) or _DEFAULT_PLAYOUT_MS
    cmd = [
        _sender_bin(), "-c", "h264", "-m", str(bitrate),
        "--audio-sink", _SINK_NAME,
        "--playout-delay", str(playout),
        f"{device}:{_CAST_PORT}", f"window:addr={addr}",
    ]  # fmt: skip
    try:
        # Detached: the sender serves for the whole runtime; it must outlive a headless
        # return and not hold the foreground.
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except (OSError, FileNotFoundError):
        return None
    return proc.pid
