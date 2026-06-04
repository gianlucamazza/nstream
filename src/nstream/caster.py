"""Chromecast playback via `catt`: device resolution, launching the cast, polling its
status for resume/auto-advance, and the in-cast audio-language switch.

Imports the fzf picker from `picker` (not cli) so there's no import cycle; cli calls
`cast()`, `resolve_device()` and `CastUnavailable`. catt is invoked with subprocess
directly (the poll loop needs returncode/stderr and a per-iteration process).
"""

from __future__ import annotations

import contextlib
import json
import select
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable

from . import languages, log, settings
from .config import Config
from .picker import fzf

_log = log.get_logger("cast")


class CastUnavailable(Exception):
    """Raised when no Chromecast can be resolved (ambiguous / none / absent)."""


# How often to poll `catt info -j` while casting (resume tracking + end detection).
# Each poll spawns a `catt` process (new castv2 connection), so keep it coarse: 15s
# costs ~240 polls over a 2h film and resume granularity of ≤15s is plenty.
_CAST_POLL = 15.0
# Fraction of the runtime past which a stop counts as "finished" (→ binge advance).
_CAST_DONE = 0.97
# Give up if the cast never starts playing within this many polls (~60s): the device
# may be unreachable or the receiver refused the media — don't poll forever.
_CAST_GIVEUP = 4


def resolve_device(
    cfg: Config, *, choose: bool = False, headless: bool = False, prefer: str | None = None
) -> str:
    """Resolve the value for `catt -d` — an **IP** from a fresh `catt scan`, so casting
    is robust to mDNS name-resolution flakiness after a network change. A configured
    `cast_device` (a stable *name*) is honoured only when present on the current LAN,
    else we re-discover. One device → use it; several (or `choose`) → pick by name (cast
    by IP). Raises CastUnavailable when the scan finds nothing reachable / the user
    cancels (the caller then falls back to local mpv).

    `headless` (non-interactive callers) never opens the fzf picker: an explicit `prefer`
    name (or `cfg.cast_device`) must be on the LAN, else a single device is used, else it
    raises CastUnavailable so the caller can surface a clean error instead of blocking."""
    devices = settings.scan_devices()  # [(name, ip)] on the *current* LAN
    by_name = dict(devices)
    # An explicit target (--device) wins, but only if actually on this LAN.
    if prefer:
        ip = by_name.get(prefer)
        if ip:
            return ip
        raise CastUnavailable(f"dispositivo '{prefer}' non in rete")
    # A saved preference is honoured only if that device is actually on this LAN —
    # so after a network change a stale name doesn't pin us to an absent device.
    if cfg.cast_device and not choose:
        ip = by_name.get(cfg.cast_device)
        if ip:
            return ip
        _log.info("device preferito '%s' non in rete → ridiscovery", cfg.cast_device)
    if not devices:
        # Trust the fresh scan: nothing here now (TV off, or a different network). We
        # deliberately don't fall back to a configured default regardless of presence —
        # that would cast to an absent device. The caller degrades to local playback.
        raise CastUnavailable("nessun Chromecast in rete")
    if len(devices) == 1 and not choose:
        return devices[0][1]  # the IP
    if headless:
        # Ambiguous LAN and no usable preference: a headless caller can't pick — surface
        # it as an error (the agent re-runs with --device) instead of opening fzf.
        names = ", ".join(name for name, _ in devices)
        raise CastUnavailable(f"più dispositivi in rete ({names}): specifica --device")
    # Several devices (or an explicit choice): pick by name, cast by IP.
    chosen = fzf([(name, ip) for name, ip in devices], "dispositivo> ")
    if chosen is None:
        raise CastUnavailable("scelta dispositivo annullata")
    return chosen


def _cast_progress(info: dict) -> tuple[float, float, str]:
    """Extract (position, duration, player_state) from `catt info -j` JSON,
    tolerating the field set varying with the receiver/app."""
    state = str(info.get("player_state") or "")
    try:
        dur = float(info.get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    pos = 0.0
    cur = info.get("current_time")
    rem = info.get("remaining")
    prog = info.get("progress")
    try:
        if cur is not None:
            pos = float(cur)
        elif rem is not None and dur:
            pos = max(0.0, dur - float(rem))
        elif prog is not None and dur:
            pos = dur * float(prog) / 100.0
    except (TypeError, ValueError):
        pos = 0.0
    return (pos, dur, state)


@contextlib.contextmanager
def _cbreak(stream):
    """Put a TTY into cbreak so single keypresses arrive without Enter, restoring
    the original attributes on exit (even on error). No-op for non-TTY streams.
    cbreak keeps ISIG enabled, so Ctrl-C still raises KeyboardInterrupt."""
    if not stream.isatty():
        yield
        return
    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _poll_wait(timeout: float) -> str | None:
    """Wait up to `timeout` for a single keypress on stdin; return the char or None.
    On a non-interactive stdin it just sleeps (same effect as the old time.sleep)."""
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    with _cbreak(sys.stdin):
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if ready:
            return sys.stdin.read(1)
    return None


def _switch_cast_audio(
    base: list[str],
    langs: tuple[str, ...],
    resolve_lang: Callable[[str], str | None],
    pos: float,
    dest: str,
) -> None:
    """Re-cast a release in the chosen audio language from the current position.
    The Chromecast plays the file's default track, so this picks a differently-dubbed
    release rather than switching tracks in place (best-effort, single-dub friendly)."""
    items = [(languages.name(lang), lang) for lang in langs]
    lang = fzf(items, "audio> ")
    if lang is None:  # ESC → keep the current cast
        return
    print(f"📺 cambio audio: {languages.name(lang)}…", file=sys.stderr)
    new = resolve_lang(lang)
    if not new:
        print(f"nstream: nessuno stream {lang} compatibile col Chromecast", file=sys.stderr)
        return
    print(f"📺 preparo il cast su {dest}…", file=sys.stderr)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([*base, "cast", new, "-t", str(int(pos))], capture_output=True, text=True)


def cast(
    cfg: Config,
    title: str,
    url: str,
    *,
    device: str | None,
    start: float | None = None,
    sub_paths: tuple[str, ...] = (),
    next_label: str | None = None,
    langs: tuple[str, ...] = (),
    resolve_lang: Callable[[str], str | None] | None = None,
    follow: bool = True,
) -> tuple[float, float, bool]:
    """Cast `url` to a Chromecast via `catt`, then poll its status so resume and
    series auto-advance work just like the mpv path. Returns (position, duration,
    advance) — same contract as `play()`.

    `advance` is True only when a next episode is queued (`next_label`) and playback
    reached the end (so a manual stop mid-episode doesn't binge ahead).

    `follow=False` (headless fire-and-return): once `catt cast` has handed the media to
    the receiver, return immediately without the resume/advance poll loop — so an agent
    isn't held for the whole runtime. No position is tracked (no resume) in that mode."""
    base = ["catt", *(["-d", device] if device else [])]
    launch = [*base, "cast", url]
    if start and start > 1:
        launch += ["-t", str(int(start))]
    if sub_paths:  # catt takes a single subtitle file
        launch += ["-s", sub_paths[0]]
    dest = device or "Chromecast"
    _log.debug("catt launch: %s", " ".join(launch))  # token redacted by the log filter
    # `catt cast` blocks while the receiver buffers the remote URL (~10s); say so.
    print(f"📺 preparo il cast su {dest}…", file=sys.stderr)
    try:
        proc = subprocess.run(launch, capture_output=True, text=True)
    except FileNotFoundError:
        print("nstream: catt non trovato", file=sys.stderr)
        return (0.0, 0.0, False)
    if proc.returncode != 0:
        # catt prints the cause (e.g. device unreachable); never echo the URL/token.
        _log.warning("cast non riuscito (rc=%s): %s", proc.returncode, proc.stderr.strip()[:300])
        print("nstream: cast non riuscito", file=sys.stderr)
        return (0.0, 0.0, False)

    if not follow:
        # Fire-and-return: the receiver has the media; don't poll for the whole runtime.
        print(f"📺 {title} → {dest}", file=sys.stderr)
        return (0.0, 0.0, False)

    can_switch = bool(langs) and resolve_lang is not None
    hint = "a: lingua audio · Ctrl-C: stop" if can_switch else "Ctrl-C per smettere di seguire"
    print(f"📺 {title} → {dest}  ({hint})", file=sys.stderr)
    holder = {"position": 0.0, "duration": 0.0}
    started = False
    finished = False
    warned_vol = False
    idle = 0  # consecutive polls without progress before playback ever starts
    try:
        while True:
            if _poll_wait(_CAST_POLL) == "a" and can_switch:
                _switch_cast_audio(base, langs, resolve_lang, holder["position"], dest)
                started, finished, idle = False, False, 0  # new media re-buffers
                continue
            res = subprocess.run([*base, "info", "-j"], capture_output=True, text=True)
            info = None
            if res.returncode == 0:
                with contextlib.suppress(json.JSONDecodeError):
                    info = json.loads(res.stdout or "{}")
            if info is None:  # device idle/unreachable or unparseable status
                if started:
                    break  # went away after playing → ended
                idle += 1
                if idle >= _CAST_GIVEUP:
                    print("nstream: il cast non è partito", file=sys.stderr)
                    break
                continue
            pos, dur, pstate = _cast_progress(info)
            if pos > 0:
                holder["position"] = pos
            if dur > 0:
                holder["duration"] = dur
            # A device left at volume 0 plays silently — explain it once.
            if not warned_vol and not info.get("volume_muted") and info.get("volume_level") == 0:
                warned_vol = True
                print(
                    "nstream: volume del Chromecast a 0 — alza col telecomando o 'catt volume N'",
                    file=sys.stderr,
                )
            if pstate in ("PLAYING", "PAUSED", "BUFFERING") or pos > 0:
                started = True
                idle = 0
            elif started and pstate in ("IDLE", "UNKNOWN", ""):
                d = holder["duration"]
                finished = bool(d) and holder["position"] >= d * _CAST_DONE
                break
            else:  # not started yet, receiver idle → wait, but not forever
                idle += 1
                if idle >= _CAST_GIVEUP:
                    print("nstream: il cast non è partito", file=sys.stderr)
                    break
    except KeyboardInterrupt:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run([*base, "stop"], capture_output=True, text=True)
    advance = bool(next_label) and finished
    return (holder["position"], holder["duration"], advance)
