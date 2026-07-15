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
import shutil
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable
from dataclasses import dataclass

from . import bridge, cast_delivery, discovery, languages, log, serve, subs, ui, util
from .config import Config
from .picker import confirm as _confirm
from .picker import fzf

_log = log.get_logger("cast")


@dataclass(frozen=True)
class CastMeta:
    """Now-playing metadata sent to the receiver (and surfaced on the HUD widget) when casting
    via castbridge. Sourced from the Cinemeta meta in `cli`. Empty fields are omitted, so the
    LOAD degrades to a Movie block (poster/subtitle) or a bare title as available."""

    poster: str = ""
    subtitle: str = ""
    series_title: str = ""
    season: int = 0
    episode: int = 0
    content_type: str = ""


# Callback the headless `--follow` JSONL path passes in to receive normalized playback events
# (started/playing/paused/ended/failed) as they happen; None for the interactive path.
# Canonical definition lives with the shared driver (ADR 0011); re-exported here because
# every cast signature historically names `caster.EventCb`.
EventCb = cast_delivery.EventCb


class CastUnavailable(Exception):
    """Raised when no Chromecast can be resolved (ambiguous / none / absent)."""


# Session latch for the auto-cast confirmation: once the user okays casting, later plays
# (binge auto-advance, next episode) don't re-ask. A refusal is per-play (asked again).
_cast_confirmed = False


def _confirm_device(name: str, ip: str) -> bool:
    """Confirm the auto-resolved cast target on an interactive tty (fzf Sì/No, default
    yes), so a `prefer_cast` route to the TV is announced instead of silent.
    Non-interactive callers and an already-confirmed session always pass."""
    global _cast_confirmed
    if _cast_confirmed or not (sys.stdin.isatty() and sys.stderr.isatty()):
        return True
    ok = _confirm(
        f"{ui.g().tv} Chromecast trovato: {name} ({ip}) — casto lì?",
        default_yes=True,
        non_tty_default=True,
    )
    _cast_confirmed = ok
    return ok


# How often to poll `catt info -j` while casting (resume tracking + end detection).
# Each poll spawns a `catt` process (new castv2 connection), so keep it coarse: 15s
# costs ~240 polls over a 2h film and resume granularity of ≤15s is plenty.
_CAST_POLL = 15.0
# Fraction of the runtime past which a stop counts as "finished" (→ binge advance).
# Single source with the bridge driver (ADR 0011); the catt poll loop shares it.
_CAST_DONE = cast_delivery.CAST_DONE
# Give up if the cast never starts playing within this many polls (~60s): the device
# may be unreachable or the receiver refused the media — don't poll forever.
_CAST_GIVEUP = 4

# Wait budgets (seconds) on the background scan. The scan usually started at TUI
# startup and is long done, so the default wait is short — cast is optional and must
# not freeze the UX. An explicit picker (Alt-C / --cast-choose) asked for the device
# list, so one full scan round is worth waiting for.
_WAIT_RESOLVE = 6.0
_WAIT_CHOOSE = 25.0


def resolve_device(
    cfg: Config,
    *,
    choose: bool = False,
    headless: bool = False,
    prefer: str | None = None,
    confirm: bool = False,
) -> str:
    """Resolve the value for `catt -d` — an **IP** from discovery (verified cache or
    the background `catt scan`), so casting is robust to mDNS name-resolution flakiness
    after a network change. A configured `cast_device` (a stable *name*) is honoured
    only when present on the current LAN, else we re-discover. One device → use it;
    several (or `choose`) → pick by name (cast by IP). Raises CastUnavailable when
    discovery finds nothing reachable / the user cancels (the caller then falls back to
    local mpv). Cast is optional: this never blocks longer than a short wait budget on
    interactive paths (Ctrl-C skips straight to local playback).

    `headless` (non-interactive callers) never opens the fzf picker: an explicit `prefer`
    name (or `cfg.cast_device`) must be on the LAN, else a single device is used, else it
    raises CastUnavailable so the caller can surface a clean error instead of blocking.

    `confirm` asks once per session ([S/n] on a tty) before an **auto**-resolved device is
    used — so a `prefer_cast` start announces where the video is going instead of silently
    casting. Explicit picks (`prefer`, the fzf picker) never re-ask; a refusal raises
    CastUnavailable so the caller falls back to local playback."""
    # A missing binary must not masquerade as an empty network: run_cmd swallows the
    # OSError, so an instant empty scan would read as "no Chromecast" when the real
    # problem is catt not being on PATH (e.g. a desktop session without ~/.local/bin).
    if shutil.which("catt") is None:
        _log.warning("catt non trovato nel PATH → cast non disponibile")
        raise CastUnavailable("catt non trovato nel PATH (pipx install catt)")
    devices = _discover(cfg, prefer=prefer, choose=choose, headless=headless)
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
            if confirm and not _confirm_device(cfg.cast_device, ip):
                raise CastUnavailable(f"cast su '{cfg.cast_device}' rifiutato")
            return ip
        _log.info("device preferito '%s' non in rete → ridiscovery", cfg.cast_device)
    if not devices:
        # Trust the fresh scan: nothing here now (TV off, or a different network). We
        # deliberately don't fall back to a configured default regardless of presence —
        # that would cast to an absent device. The caller degrades to local playback.
        raise CastUnavailable("nessun Chromecast in rete")
    if len(devices) == 1 and not choose:
        name, ip = devices[0]
        if confirm and not _confirm_device(name, ip):
            raise CastUnavailable(f"cast su '{name}' rifiutato")
        return ip
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


def _discover(
    cfg: Config, *, prefer: str | None, choose: bool, headless: bool
) -> list[discovery.Device]:
    """Devices for `resolve_device`, without freezing the UX. A cache-verified target
    (or the single cached device) is used instantly — a live TCP connection to the cast
    port beats waiting on a fresh mDNS scan. Otherwise lean on the background scan
    (kicked off at TUI startup; started here for Alt-C/headless), waiting only a short
    budget on interactive paths — Ctrl-C skips the wait. An empty or late scan falls
    back to cached devices that still answer before giving up."""
    cached = discovery.load_cache()
    target = prefer or cfg.cast_device
    if target:
        ip = dict(cached).get(target)
        if ip and discovery.verify(ip):
            return [(target, ip)]
    elif not choose and len(cached) == 1 and discovery.verify(cached[0][1]):
        # The common single-TV home: instant cast/--status/--stop across sessions.
        # A multi-device cache with no preference falls through instead — only a
        # fresh scan should feed the picker.
        return list(cached)
    discovery.start_background()
    devices, state = discovery.get_devices(wait=0.0)
    if state == "pending":
        hint = "" if headless else " (Ctrl-C: riproduci in locale)"
        print(f"{ui.g().search} cerco Chromecast…{hint}", file=sys.stderr)
        wait = None if headless else (_WAIT_CHOOSE if choose else _WAIT_RESOLVE)
        try:
            devices, state = discovery.get_devices(wait=wait)
        except KeyboardInterrupt:
            raise CastUnavailable("ricerca dispositivi annullata") from None
    if devices:
        return devices
    # Fresh scan empty (or out of budget): rescue any cached device that still answers.
    return [(name, ip) for name, ip in cached if discovery.verify(ip)]


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
    print(f"{ui.g().tv} cambio audio: {languages.name(lang)}…", file=sys.stderr)
    new = resolve_lang(lang)
    if not new:
        print(f"nstream: nessuno stream {lang} compatibile col Chromecast", file=sys.stderr)
        return
    print(f"{ui.g().tv} preparo il cast su {dest}…", file=sys.stderr)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [*base, "cast", new, "-t", str(int(pos))],
            capture_output=True,
            text=True,
            timeout=util.CATT_CAST_TIMEOUT,
        )


def cast(
    cfg: Config,
    title: str,
    url: str,
    *,
    device: str | None,
    start: float | None = None,
    sub_paths: tuple[str, ...] = (),
    sub_lang: str | None = None,
    next_label: str | None = None,
    langs: tuple[str, ...] = (),
    resolve_lang: Callable[[str], str | None] | None = None,
    follow: bool = True,
    meta: CastMeta | None = None,
    on_event: EventCb | None = None,
) -> tuple[float, float, bool, bool]:
    """Cast `url` to a Chromecast and track playback so resume and series auto-advance work like
    the mpv path. Returns (position, duration, advance, subs_delivered). Prefers the **castbridge**
    native sender (metadata-rich LOAD + a real event stream) when its binary is available; falls
    back to **catt** (no metadata) otherwise, when castbridge can't start, or for the interactive
    in-cast audio switch ('a'), which remains a catt-path capability (see docs/adr/0007).
    `on_event` receives normalized events for the headless `--follow` JSONL path.

    `subs_delivered`: whether the requested `sub_paths` were actually attached to the cast. Both
    senders now carry subtitles: the castbridge path serves the SRT as a side-loaded WebVTT track
    (`sub_lang` labels it), the catt path uses `-s`."""
    can_switch = bool(langs) and resolve_lang is not None and follow and sys.stdin.isatty()
    if device and bridge.bridge_available() and not can_switch:
        result = _cast_via_bridge(
            title,
            url,
            device=device,
            start=start,
            meta=meta or CastMeta(),
            next_label=next_label,
            follow=follow,
            on_event=on_event,
            sub_paths=sub_paths,
            sub_lang=sub_lang,
        )
        if result is not None:
            return result  # else castbridge couldn't start → fall back to catt below
    catt_result = _cast_via_catt(
        cfg,
        title,
        url,
        device=device,
        start=start,
        sub_paths=sub_paths,
        next_label=next_label,
        langs=langs,
        resolve_lang=resolve_lang,
        follow=follow,
        on_event=on_event,
    )
    return (*catt_result, bool(sub_paths))


def _cast_via_bridge(
    title: str,
    url: str,
    *,
    device: str,
    start: float | None,
    meta: CastMeta,
    next_label: str | None,
    follow: bool,
    on_event: EventCb | None,
    sub_paths: tuple[str, ...] = (),
    sub_lang: str | None = None,
) -> tuple[float, float, bool, bool] | None:
    """Cast via castbridge with metadata, forwarding normalized events to `on_event`. Returns
    (position, duration, advance, subs_delivered), or **None** when the cast never started
    (transport/daemon failure) so the caller falls back to catt. A media error the receiver
    reports (bad url/device) ends as a `failed` event without a fallback (catt wouldn't fare
    better).

    A direct cast plays the remote `url`, so a requested subtitle rides a small local server of
    its own (the SRT converted to WebVTT), side-loaded as an active caption track. `follow` keeps
    that server in-process; a fire-and-return detaches it (single-slot, reaped by the next cast /
    `--stop`)."""
    kwargs = {
        "title": title,
        "poster": meta.poster,
        "subtitle": meta.subtitle,
        "series_title": meta.series_title,
        "season": meta.season,
        "episode": meta.episode,
        "content_type": meta.content_type,
        "current_time": float(start or 0.0),
    }
    vtt = subs.to_vtt(sub_paths[0]) if sub_paths else None
    sub_shutdown, sub_delivered = _serve_subtitle(vtt, device, sub_lang, follow, kwargs)

    def announce() -> None:
        tail = "  (Ctrl-C per smettere di seguire)" if follow else ""
        print(f"{ui.g().tv} {title} → {device}{tail}", file=sys.stderr)

    # Shared driver (ADR 0011). No on_interrupt hook: interactive semantics — Ctrl-C
    # stops following (the driver already stopped the receiver) and keeps the result.
    try:
        out = cast_delivery.drive_bridge(
            device,
            url,
            follow=follow,
            load_kwargs=kwargs,
            on_event=on_event,
            on_started=announce,
        )
    finally:
        if sub_shutdown is not None:  # in-process (follow) server: tear down with the cast
            sub_shutdown()
    if out is None:
        return None
    advance = bool(next_label) and out.finished
    return (out.pos, out.dur, advance, sub_delivered)


def _serve_subtitle(
    vtt: str | None, device: str, sub_lang: str | None, follow: bool, kwargs: dict
) -> tuple[Callable[[], None] | None, bool]:
    """Serve `vtt` (if any) for a Tier-1 direct cast and add its URL to `kwargs`. Returns
    `(shutdown_callable_or_None, subs_delivered)`. `follow` → an in-process server whose
    `shutdown` the caller must call; fire-and-return → a detached single-slot server (no
    shutdown callable — reaped by the next cast / `--stop`)."""
    if not vtt:
        return None, False
    bind_ip = serve.lan_ip(device)
    serve.ensure_firewall(bind_ip)
    serve.reap_sub_server()  # only one cast plays at a time → drop any leftover VTT server
    if follow:
        server, port, _thread = serve.serve_file(None, bind_ip, sub_path=vtt)
        kwargs["subtitle_url"] = serve.served_sub_url(bind_ip, port, server.token)
        if sub_lang:
            kwargs["subtitle_lang"] = sub_lang
        return server.shutdown, True
    spawned = serve.spawn_detached(bind_ip, sub_path=vtt)
    if spawned is None:
        return None, False
    pid, port, token = spawned
    serve.register_sub_server(pid)
    kwargs["subtitle_url"] = serve.served_sub_url(bind_ip, port, token)
    if sub_lang:
        kwargs["subtitle_lang"] = sub_lang
    return None, True


def _emit(on_event: EventCb | None, kind: str, **fields) -> None:
    """Forward a normalized event to the `--follow` JSONL callback, if any."""
    if on_event:
        on_event({"kind": kind, **fields})


def _cast_via_catt(
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
    on_event: EventCb | None = None,
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
    # Never log the URL itself: the redaction regexes cover the known token carriers, but a
    # signed native-CDN link (TorBox/Premiumize requestdl) is a capability in its own right
    # and its query params don't necessarily match them.
    _log.debug("catt launch: %s", " ".join(a if a != url else "<url>" for a in launch))
    # `catt cast` blocks while the receiver buffers the remote URL (~10s); say so.
    print(f"{ui.g().tv} preparo il cast su {dest}…", file=sys.stderr)
    try:
        proc = subprocess.run(
            launch, capture_output=True, text=True, timeout=util.CATT_CAST_TIMEOUT
        )
    except FileNotFoundError:
        print("nstream: catt non trovato", file=sys.stderr)
        _emit(on_event, "failed", error="catt_missing", message="catt non trovato")
        return (0.0, 0.0, False)
    except subprocess.TimeoutExpired:
        # A catt hung on a half-dead device must not block the caller forever.
        _log.warning("catt cast bloccato oltre %.0fs → annullato", util.CATT_CAST_TIMEOUT)
        print("nstream: cast non riuscito (timeout)", file=sys.stderr)
        _emit(on_event, "failed", error="cast_timeout", message="cast non riuscito (timeout)")
        return (0.0, 0.0, False)
    if proc.returncode != 0:
        # catt prints the cause (e.g. device unreachable); never echo the URL/token.
        _log.warning("cast non riuscito (rc=%s): %s", proc.returncode, proc.stderr.strip()[:300])
        print("nstream: cast non riuscito", file=sys.stderr)
        _emit(on_event, "failed", error="cast_failed", message="cast non riuscito")
        return (0.0, 0.0, False)

    if not follow:
        # Fire-and-return: the receiver has the media; don't poll for the whole runtime.
        print(f"{ui.g().tv} {title} → {dest}", file=sys.stderr)
        _emit(on_event, "started", title=title)
        return (0.0, 0.0, False)

    can_switch = bool(langs) and resolve_lang is not None
    hint = "a: lingua audio · Ctrl-C: stop" if can_switch else "Ctrl-C per smettere di seguire"
    print(f"{ui.g().tv} {title} → {dest}  ({hint})", file=sys.stderr)
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
            try:
                res = subprocess.run(
                    [*base, "info", "-j"],
                    capture_output=True,
                    text=True,
                    timeout=util.CATT_INFO_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                res = None  # a hung poll counts as an unreachable device
            info = None
            if res is not None and res.returncode == 0:
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
                if not started:
                    _emit(on_event, "started", title=title)
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
            subprocess.run(
                [*base, "stop"], capture_output=True, text=True, timeout=util.CATT_INFO_TIMEOUT
            )
    advance = bool(next_label) and finished
    if started:
        _emit(
            on_event,
            "ended",
            position=round(holder["position"], 1),
            duration=round(holder["duration"], 1),
        )
    return (holder["position"], holder["duration"], advance)


def _raw_info(device: str | None) -> dict:
    """One `catt info -j`, parsed; {} on any failure (best-effort, never raises)."""
    base = ["catt", *(["-d", device] if device else [])]
    try:
        res = subprocess.run(
            [*base, "info", "-j"], capture_output=True, text=True, timeout=util.CATT_INFO_TIMEOUT
        )
        return json.loads(res.stdout or "{}")
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError):
        return {}


def device_volume(device: str | None) -> tuple[float | None, bool]:
    """Best-effort (volume_level, volume_muted) from one `catt info -j`. For headless
    fire-and-return casts that skip the poll loop and would otherwise miss a muted or
    zero-volume receiver (a silent cast that looks fine). Never raises."""
    info = _raw_info(device)
    raw = info.get("volume_level")
    try:
        vol = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        vol = None
    return (vol, bool(info.get("volume_muted")))


def _bridge_track_info(device: str | None) -> tuple[list[int], str | None]:
    """The receiver's confirmed active track ids + any error, read from the castbridge
    session (ADR 0016) — the receiver's own view, which catt's status can't see. ([], None)
    when the bridge isn't running (no live cast) or reports nothing; never spawns the daemon
    just to answer, and never raises."""
    if not bridge.bridge_available():
        return [], None
    data = bridge.peek_status(device)
    media = data.get("media") if isinstance(data, dict) else None
    if not isinstance(media, dict):
        return [], None
    ids = media.get("activeTrackIds")
    tracks = [t for t in ids if isinstance(t, int)] if isinstance(ids, list) else []
    err = media.get("error")
    return tracks, (str(err) if err else None)


def status(device: str | None) -> dict:
    """Best-effort normalized receiver status for the headless `--status` action:
    player_state, title, position, duration, volume, muted, plus the receiver's confirmed
    active_tracks + receiver_error (from castbridge, ADR 0016). Empty player_state when the
    receiver is idle/unreachable. Never raises."""
    info = _raw_info(device)
    pos, dur, state = _cast_progress(info)
    title = (info.get("media_metadata") or {}).get("title") or info.get("title") or None
    vol, muted = device_volume(device) if not info else _vol_muted(info)
    active_tracks, receiver_error = _bridge_track_info(device)
    return {
        "player_state": state or "IDLE",
        "title": title,
        "position": round(pos, 1) if pos else 0.0,
        "duration": round(dur, 1) if dur else 0.0,
        "volume": vol,
        "muted": muted,
        "active_tracks": active_tracks,
        "receiver_error": receiver_error,
    }


def _vol_muted(info: dict) -> tuple[float | None, bool]:
    raw = info.get("volume_level")
    try:
        vol = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        vol = None
    return (vol, bool(info.get("volume_muted")))


def stop(device: str | None) -> bool:
    """Stop whatever the receiver is playing (`catt stop`). True on success; best-effort."""
    base = ["catt", *(["-d", device] if device else [])]
    try:
        res = subprocess.run(
            [*base, "stop"], capture_output=True, text=True, timeout=util.CATT_INFO_TIMEOUT
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def set_volume(device: str | None, level: int) -> bool:
    """Set the receiver volume to `level` (0–100) via `catt volume`. Best-effort."""
    level = max(0, min(100, level))
    base = ["catt", *(["-d", device] if device else [])]
    try:
        res = subprocess.run(
            [*base, "volume", str(level)],
            capture_output=True,
            text=True,
            timeout=util.CATT_INFO_TIMEOUT,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
