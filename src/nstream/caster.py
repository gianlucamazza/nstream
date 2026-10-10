"""Chromecast playback via `catt`: device resolution, launching the cast, polling its
status for resume/auto-advance, and the in-cast audio-language switch.

Menus are injected by the frontend (ADR 0037): `resolve_device(confirm=, picker=)` and
`cast(choose_lang=)`; this module never imports the fzf picker. cli calls `cast()`,
`resolve_device()` and `CastUnavailable`. catt is invoked with subprocess
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

from . import bridge, cast_delivery, discovery, languages, log, notices, serve, srt, ui, util
from .config import Config

_log = log.get_logger("cast")


@dataclass(frozen=True)
class CastMeta:
    """Now-playing metadata for the receiver LOAD. castbridge sends the full block
    (Movie/TvShow + poster). The catt 0.13 CLI fallback sends title + streamType only
    (`-l`, `--stream-type`); it has no `--thumb` (ADR 0050). Sourced from Cinemeta in
    `cli` / `headless`. Empty fields are omitted."""

    poster: str = ""
    subtitle: str = ""
    series_title: str = ""
    season: int = 0
    episode: int = 0
    content_type: str = ""


# catt 0.13 DefaultCastController.play_media_url → pychromecast play_media.
# CLI exposes `-l/--title` and `--stream-type`; not `--thumb` / metadataType.
CATT_STREAM_BUFFERED = "BUFFERED"
CATT_METADATA_GENERIC = 0  # pychromecast METADATA_TYPE_GENERIC when metadataType omitted


def catt_display_title(title: str, meta: CastMeta | None = None) -> str:
    """Single-line title for catt `-l`. The CLI has no TvShow block, so series become
    `Show · SxxEyy · episode` (same shape as `labels.display_title`, no labels import)."""
    name = " ".join((title or "").split())
    if not meta:
        return name
    show = " ".join((meta.series_title or "").split())
    if not show:
        return name
    if meta.season > 0 and meta.episode > 0:
        ep = f"S{meta.season:02d}E{meta.episode:02d}"
        if name and name != show:
            return f"{show} · {ep} · {name}"
        return f"{show} · {ep}"
    return show


def catt_cast_argv(
    device: str | None,
    source: str,
    *,
    title: str = "",
    start: float | None = None,
    sub_path: str | None = None,
    stream_type: str = CATT_STREAM_BUFFERED,
) -> list[str]:
    """`catt cast` argv for a URL or local remux file (ADR 0050).

    `-l` overrides catt's local-file stem / yt-dlp title. `--stream-type BUFFERED` is
    required for a complete remux: catt's StreamInfo only infers BUFFERED on a remote
    URL that yt-dlp gave a duration, so a `cast-*.mp4` would otherwise LOAD with
    `streamType: null` (pychromecast default LIVE is not applied when the kwarg is
    None). No `--thumb`: catt 0.13 has none. Never put a debrid URL in logs — callers
    redact `source` themselves.
    """
    args = ["catt", *(["-d", device] if device else []), "cast", source]
    if start and start > 1:
        args += ["-t", str(int(start))]
    if sub_path:
        args += ["-s", sub_path]
    label = " ".join(title.split()) if title else ""
    if label:
        args += ["-l", label]
    if stream_type:
        args += ["--stream-type", stream_type]
    return args


def catt_media_info(
    *,
    title: str = "",
    poster: str = "",
    content_type: str = "video/mp4",
    stream_type: str = CATT_STREAM_BUFFERED,
    content_id: str = "",
) -> dict:
    """MediaInformation body catt 0.13 / pychromecast `play_media` puts on LOAD.

    Reverse-engineered from `DefaultCastController.play_media_url` (catt v0.13.3) and
    pychromecast `MediaController._send_start_play_media`. `content_id` is the Cast
    `contentId` (the media URL). Tests pass a label, never a debrid URL. `poster`
    becomes `metadata.thumb` + `images[0].url` — the CLI cannot set it; castbridge
    can (`bridge._media_load_args`). metadataType is GENERIC (0) unless a caller
    upgrades it; Movie/TvShow is a castbridge-only block.
    """
    metadata: dict = {}
    label = " ".join(title.split()) if title else ""
    if label:
        metadata["title"] = label
    image = (poster or "").strip()
    if image:
        metadata["thumb"] = image
        metadata["images"] = [{"url": image}]
    if metadata:
        metadata["metadataType"] = CATT_METADATA_GENERIC
    media: dict = {
        "streamType": stream_type or CATT_STREAM_BUFFERED,
        "contentType": content_type or "video/mp4",
        "metadata": metadata,
    }
    if content_id:
        media["contentId"] = content_id
    return media


def catt_cli_media_info(
    title: str,
    meta: CastMeta | None = None,
    *,
    content_type: str = "video/mp4",
    stream_type: str = CATT_STREAM_BUFFERED,
    content_id: str = "",
) -> dict:
    """LOAD MediaInformation the catt **CLI** can actually send (no poster)."""
    return catt_media_info(
        title=catt_display_title(title, meta),
        content_type=(meta.content_type if meta and meta.content_type else content_type),
        stream_type=stream_type,
        content_id=content_id,
    )


# Callback the headless `--follow` JSONL path passes in to receive normalized playback events
# (started/playing/paused/ended/failed) as they happen; None for the interactive path.
# Canonical definition lives with the shared driver (ADR 0011); re-exported here because
# every cast signature historically names `caster.EventCb`.
EventCb = cast_delivery.EventCb


class CastUnavailable(Exception):
    """Raised when no Chromecast can be resolved (ambiguous / none / absent)."""


# Frontend prompts injected by the caller (ADR 0037): the domain never opens fzf.
ConfirmDevice = Callable[[str, str], bool]  # (name, ip) -> cast there?
ChooseDevice = Callable[[list[tuple[str, str]]], str | None]  # [(name, ip)] -> ip | None (ESC)
ChooseLang = Callable[[tuple[str, ...]], str | None]  # dub codes -> code | None (ESC)


# How often to poll `catt info -j` while casting (resume tracking + end detection).
# Each poll spawns a `catt` process (new castv2 connection), so keep it coarse: 15s
# costs ~240 polls over a 2h film and resume granularity of ≤15s is plenty.
_CAST_POLL = 15.0
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
    confirm: ConfirmDevice | None = None,
    picker: ChooseDevice | None = None,
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

    `confirm(name, ip)` (the frontend's prompt, ADR 0037) is asked before an
    **auto**-resolved device is used — so a `prefer_cast` start announces where the video is
    going instead of silently casting. `picker(devices)` chooses among several; without one
    an ambiguous LAN raises like headless. Explicit picks (`prefer`, the picker) never
    re-ask; a refusal raises
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
            if confirm is not None and not confirm(cfg.cast_device, ip):
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
        if confirm is not None and not confirm(name, ip):
            raise CastUnavailable(f"cast su '{name}' rifiutato")
        return ip
    if headless or picker is None:
        # Ambiguous LAN and no usable preference: a caller without a picker can't choose —
        # surface it as an error (the agent re-runs with --device) instead of blocking.
        names = ", ".join(name for name, _ in devices)
        raise CastUnavailable(f"più dispositivi in rete ({names}): specifica --device")
    # Several devices (or an explicit choice): pick by name, cast by IP.
    chosen = picker(list(devices))
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
    choose_lang: ChooseLang,
    pos: float,
    dest: str,
) -> None:
    """Re-cast a release in the chosen audio language from the current position.
    The Chromecast plays the file's default track, so this picks a differently-dubbed
    release rather than switching tracks in place (best-effort, single-dub friendly)."""
    lang = choose_lang(langs)
    if lang is None:  # ESC → keep the current cast
        return
    print(f"{ui.g().tv} cambio audio: {languages.name(lang)}…", file=sys.stderr)
    new = resolve_lang(lang)
    if not new:
        notices.emit(f"nessuno stream {lang} compatibile col Chromecast")
        return
    print(f"{ui.g().tv} preparo il cast su {dest}…", file=sys.stderr)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [*base, "cast", new, "-t", str(int(pos))],
            capture_output=True,
            text=True,
            timeout=util.CATT_CAST_TIMEOUT,
        )


@log.phase("cast_direct")
def cast(
    cfg: Config,
    title: str,
    url: str,
    *,
    device: str | None,
    start: float | None = None,
    sub_paths: tuple[str, ...] = (),
    sub_lang: str | None = None,
    langs: tuple[str, ...] = (),
    resolve_lang: Callable[[str], str | None] | None = None,
    choose_lang: ChooseLang | None = None,
    follow: bool = True,
    meta: CastMeta | None = None,
    on_event: EventCb | None = None,
) -> cast_delivery.CastResult:
    """Cast `url` to a Chromecast and track playback so resume and series auto-advance work like
    the mpv path. Returns a `CastResult` — read it by attribute (ADR 0031). The advance decision
    belongs to `cast_flow`, not to a delivery backend (ADR 0029).     Prefers the **castbridge**
    native sender (Movie/TvShow LOAD + poster + a real event stream) when its binary is
    available; falls back to **catt** (`-l` title + `--stream-type BUFFERED`; no poster —
    the 0.13 CLI has no `--thumb`, ADR 0050) otherwise, when castbridge can't start, or
    for the interactive in-cast audio switch ('a') (see docs/adr/0007, docs/adr/0050).
    `on_event` receives normalized events for the headless `--follow` JSONL path.

    `subs_delivered`: whether the requested `sub_paths` were actually attached to the cast. Both
    senders now carry subtitles: the castbridge path serves the SRT as a side-loaded WebVTT track
    (`sub_lang` labels it), the catt path uses `-s`."""
    # The in-cast switch needs a frontend menu (`choose_lang`, ADR 0037); stdin being a TTY
    # is only the capability to read the 'a' keypress (`_poll_wait`), not the policy.
    can_switch = bool(langs) and resolve_lang is not None and choose_lang is not None and follow
    if device and bridge.bridge_available() and not can_switch:
        result = _cast_via_bridge(
            title,
            url,
            device=device,
            start=start,
            meta=meta or CastMeta(),
            follow=follow,
            on_event=on_event,
            sub_paths=sub_paths,
            sub_lang=sub_lang,
            app_id=(cfg.cast_receiver_app_id or "").strip(),
        )
        if result is not None:
            return result  # else castbridge couldn't start → fall back to catt below
    warn_catt_ignores_app_id(cfg)
    catt_result = _cast_via_catt(
        cfg,
        title,
        url,
        device=device,
        start=start,
        sub_paths=sub_paths,
        langs=langs,
        resolve_lang=resolve_lang,
        choose_lang=choose_lang,
        follow=follow,
        on_event=on_event,
        meta=meta,
    )
    # Explicit construction, never `(*catt_result, …)`: splatting a NamedTuple flattens it
    # into a wider plain tuple, losing both the type and the arity with no error here (ADR 0031).
    return catt_result._replace(subs_delivered=bool(sub_paths))


def _cast_via_bridge(
    title: str,
    url: str,
    *,
    device: str,
    start: float | None,
    meta: CastMeta,
    follow: bool,
    on_event: EventCb | None,
    sub_paths: tuple[str, ...] = (),
    sub_lang: str | None = None,
    app_id: str = "",
) -> cast_delivery.CastResult | None:
    """Cast via castbridge with metadata, forwarding normalized events to `on_event`. Returns
    a `CastResult`, or **None** when the cast never started
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
    if app_id:
        kwargs["app_id"] = app_id
        notices.emit(f"ricevitore custom {app_id}")
    vtt = srt.to_vtt(sub_paths[0]) if sub_paths else None
    sub_shutdown = _serve_subtitle(vtt, device, sub_lang, follow, kwargs)

    def announce() -> None:
        # Title already printed as the play banner in the interactive path; keep it for
        # Alt-C / paths that jump straight to cast without that banner.
        ui.cast_live(device, follow=follow)

    def abort(started: bool) -> bool:
        # Fire-and-return (headless) Ctrl-C is a user abort, like the Tier-2 path: drop the
        # detached subtitle server and re-raise, so `cast` does not fall back to catt
        # re-casting what was just cancelled (the swallowed interrupt used to do exactly
        # that). Following (interactive) keeps the "stop following" semantics.
        if follow:
            return False
        serve.reap_sub_server()
        return True

    # Shared driver (ADR 0011).
    try:
        out = cast_delivery.drive_bridge(
            device,
            url,
            follow=follow,
            load_kwargs=kwargs,
            on_event=on_event,
            on_started=announce,
            on_interrupt=abort,
        )
    finally:
        if sub_shutdown is not None:  # in-process (follow) server: tear down with the cast
            sub_shutdown()
    if out is None:
        return None
    # The bridge reached the receiver: `out.started` is an observation, not an assumption.
    return cast_delivery.CastResult(
        out.pos, out.dur, cast_delivery.caption_active(kwargs, out.tracks),
        started=out.started,
        error=None if out.started else (out.error or "cast_never_started"),
    )  # fmt: skip


def _serve_subtitle(
    vtt: str | None, device: str, sub_lang: str | None, follow: bool, kwargs: dict
) -> Callable[[], None] | None:
    """Serve `vtt` (if any) for a Tier-1 direct cast and add its caption-track args to
    `kwargs` (their presence is what `caption_active` reads). `follow` → an in-process
    server whose returned `shutdown` the caller must call; fire-and-return → a detached
    single-slot server (None returned — reaped by the next cast / `--stop`)."""
    if not vtt:
        return None
    bind_ip = serve.lan_ip(device)
    serve.ensure_firewall(bind_ip)
    serve.reap_sub_server()  # only one cast plays at a time → drop any leftover VTT server
    if follow:
        server, port, _thread = serve.serve_file(None, bind_ip, sub_path=vtt)
        kwargs.update(serve.caption_kwargs(bind_ip, port, server.token, sub_lang))
        return server.shutdown
    # The VTT lives in the per-play work_dir, which dies with this process, while the
    # detached server opens it PER REQUEST and the receiver re-fetches the track (seek):
    # serve a persisted copy instead, reaped together with the server.
    persisted = serve.persist_sub(vtt)
    spawned = serve.spawn_detached(bind_ip, sub_path=persisted or vtt)
    if spawned is None:
        return None
    pid, port, token = spawned
    serve.register_sub_server(pid, persisted)
    kwargs.update(serve.caption_kwargs(bind_ip, port, token, sub_lang))
    return None


def catt_sub(sub_path: str) -> str:
    """The subtitle file to hand catt: our cleaned UTF-8 WebVTT. catt's own SRT path reads
    the file as UTF-8-or-ISO-8859-15 (curly quotes and ellipses of a CP1252 file turn into
    garbage) and converts by regex, keeping the ASS/`<font>` tags a receiver shows or
    chokes on. Falls back to the original file when the conversion fails."""
    return srt.to_vtt(sub_path) or sub_path


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
    langs: tuple[str, ...] = (),
    resolve_lang: Callable[[str], str | None] | None = None,
    choose_lang: ChooseLang | None = None,
    follow: bool = True,
    on_event: EventCb | None = None,
    meta: CastMeta | None = None,
) -> cast_delivery.CastResult:
    """Cast `url` to a Chromecast via `catt`, then poll its status so resume and
    series auto-advance work just like the mpv path. Returns a `CastResult` whose `started`
    separates a real handoff from a failure — both used to be `(0.0, 0.0)` (ADR 0031).
    `cast_flow` turns pos/dur into the advance decision (ADR 0029).

    The LOAD carries `-l` title and `--stream-type BUFFERED` (ADR 0050). Poster is
    omitted: catt 0.13 has no `--thumb`. `meta.content_type` is not a CLI flag —
    catt guesses from the path; a remux `.mp4` is `video/mp4`.

    `follow=False` (headless fire-and-return): once `catt cast` has handed the media to
    the receiver, return immediately without the resume poll loop — so an agent isn't
    held for the whole runtime. No position is tracked (no resume) in that mode."""
    base = ["catt", *(["-d", device] if device else [])]
    launch = catt_cast_argv(
        device,
        url,
        title=catt_display_title(title, meta),
        start=start,
        sub_path=catt_sub(sub_paths[0]) if sub_paths else None,
    )
    dest = device or "Chromecast"
    # Never log the URL itself: the redaction regexes cover the known token carriers, but a
    # signed native-CDN link (TorBox/Premiumize requestdl) is a capability in its own right
    # and its query params don't necessarily match them.
    _log.debug("catt launch: %s", " ".join(a if a != url else "<url>" for a in launch))
    # `catt cast` blocks while the receiver buffers the remote URL (~10s); say so.
    ui.status(f"consegno a {dest}…", kind="tv")
    try:
        proc = subprocess.run(
            launch, capture_output=True, text=True, timeout=util.CATT_CAST_TIMEOUT
        )
    except FileNotFoundError:
        notices.emit("catt non trovato")
        _emit(on_event, "failed", error="catt_missing", message="catt non trovato")
        return cast_delivery.CastResult(0.0, 0.0, error="catt_missing")
    except subprocess.TimeoutExpired:
        # A catt hung on a half-dead device must not block the caller forever.
        _log.warning("catt cast bloccato oltre %.0fs → annullato", util.CATT_CAST_TIMEOUT)
        notices.emit("cast non riuscito (timeout)")
        _emit(on_event, "failed", error="cast_timeout", message="cast non riuscito (timeout)")
        return cast_delivery.CastResult(0.0, 0.0, error="cast_timeout")
    if proc.returncode != 0:
        # catt prints the cause (e.g. device unreachable); never echo the URL/token.
        _log.warning("cast non riuscito (rc=%s): %s", proc.returncode, proc.stderr.strip()[:300])
        notices.emit("cast non riuscito")
        _emit(on_event, "failed", error="cast_failed", message="cast non riuscito")
        return cast_delivery.CastResult(0.0, 0.0, error="cast_failed")

    can_switch = bool(langs) and resolve_lang is not None and choose_lang is not None
    if can_switch and follow:
        ui.cast_live(dest, follow=True)
        ui.status_detail("a: cambia lingua audio")
    else:
        ui.cast_live(dest, follow=follow)
    if not follow:
        # Fire-and-return: the receiver has the media; don't poll for the whole runtime.
        _emit(on_event, "started", title=title)
        # `catt cast` returned rc 0: the receiver ACCEPTED the handoff. That is the strongest
        # evidence available without a poll loop, so it counts as started (ADR 0031).
        return cast_delivery.CastResult(0.0, 0.0, started=True)

    holder = {"position": 0.0, "duration": 0.0}
    started = False
    warned_vol = False
    idle = 0  # consecutive polls without progress before playback ever starts
    try:
        while True:
            if _poll_wait(_CAST_POLL) == "a" and can_switch:
                _switch_cast_audio(base, langs, resolve_lang, choose_lang, holder["position"], dest)
                started, idle = False, 0  # new media re-buffers
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
                    notices.emit("il cast non è partito")
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
                notices.emit(
                    "volume del Chromecast a 0 — alza col telecomando o 'catt volume N'",
                )
            if pstate in ("PLAYING", "PAUSED", "BUFFERING") or pos > 0:
                if not started:
                    _emit(on_event, "started", title=title)
                started = True
                idle = 0
            elif started and pstate in ("IDLE", "UNKNOWN", ""):
                break  # playback ended; `cast_flow` decides if that was a natural finish
            else:  # not started yet, receiver idle → wait, but not forever
                idle += 1
                if idle >= _CAST_GIVEUP:
                    notices.emit("il cast non è partito")
                    break
    except KeyboardInterrupt:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [*base, "stop"], capture_output=True, text=True, timeout=util.CATT_INFO_TIMEOUT
            )
    if started:
        _emit(
            on_event,
            "ended",
            position=round(holder["position"], 1),
            duration=round(holder["duration"], 1),
        )
    return cast_delivery.CastResult(
        holder["position"], holder["duration"],
        started=started, error=None if started else "cast_never_started",
    )  # fmt: skip


@log.phase("catt_info")
def receiver_info(device: str | None) -> dict:
    """One `catt info -j`, parsed; {} on any failure (best-effort, never raises)."""
    base = ["catt", *(["-d", device] if device else [])]
    try:
        res = subprocess.run(
            [*base, "info", "-j"], capture_output=True, text=True, timeout=util.CATT_INFO_TIMEOUT
        )
        value = json.loads(res.stdout or "{}")
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError):
        return {}


CAST_VOLUME_PERCENT_MIN = 0
CAST_VOLUME_PERCENT_MAX = 100


def clamp_volume_percent(level: int | float) -> int:
    """CLI / catt Cast percent, clamped to 0–100 (ADR 0045)."""
    try:
        n = int(level)
    except (TypeError, ValueError):
        n = 0
    return max(CAST_VOLUME_PERCENT_MIN, min(CAST_VOLUME_PERCENT_MAX, n))


def volume_percent_to_level(percent: int | float) -> float:
    """CLI Cast percent 0–100 → protocol `SET_VOLUME` 0–1."""
    return clamp_volume_percent(percent) / 100.0


def volume_level_to_percent(level: float | int | None) -> int | None:
    """Receiver `volume_level` 0–1 → rounded Cast percent 0–100.

    Round, do not truncate: `0.14 * 100` is `13.999…` in IEEE, and Phase 0
    quantization is the *receiver* readback (`≈0.133` → 13), not that float error.
    Values already in percent (`> 1`) are clamped as 0–100. `None` if unreadable.
    """
    if level is None:
        return None
    try:
        raw = float(level)
    except (TypeError, ValueError):
        return None
    if raw > 1.0:
        return clamp_volume_percent(raw)
    return clamp_volume_percent(round(raw * 100))


def format_volume_bits(
    level: float | int | None,
    *,
    control_type: str | None = None,
    step_interval: float | None = None,
    osd_mismatch: bool = True,
) -> str | None:
    """One status fragment: `vol 13% · master · step=null · ≠ OSD`.

    `step=null` is named when the receiver is MASTER and catt omitted
    `volume_step_interval` (Philips 43PUS9235/12 DMR, ADR 0045 Phase 0).
    Cast % is never TV OSD — no conversion factor.
    """
    pct = volume_level_to_percent(level)
    if pct is None:
        return None
    bits = [f"vol {pct}%"]
    kind = (control_type or "").strip().lower()
    if kind:
        bits.append(kind)
    if kind == "master" and step_interval is None:
        bits.append("step=null")
    elif step_interval is not None:
        bits.append(f"step={step_interval:g}")
    if osd_mismatch:
        bits.append("≠ OSD")
    return " · ".join(bits)


def device_volume(device: str | None) -> tuple[float | None, bool]:
    """Best-effort (volume_level, volume_muted) from one `catt info -j`. For headless
    fire-and-return casts that skip the poll loop and would otherwise miss a muted or
    zero-volume receiver (a silent cast that looks fine). Never raises."""
    return _vol_muted(receiver_info(device))


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


def warn_catt_ignores_app_id(cfg: Config) -> None:
    """catt 0.13 launches the Default Media Receiver only. A configured custom id is a
    castbridge `media-load` argument (ADR 0013) — say so when the catt path is the one
    that will run (ADR 0045). No-op when the id is empty."""
    app_id = (cfg.cast_receiver_app_id or "").strip()
    if app_id:
        notices.emit(
            f"ricevitore custom {app_id} richiede castbridge — catt lancia CC1AD845",
            code="receiver_app_ignored",
        )


def status(device: str | None) -> dict:
    """Best-effort normalized receiver status for the headless `--status` action:
    player_state, title, position, duration, volume (0–1), volume_percent (0–100),
    muted, plus the receiver's confirmed active_tracks + receiver_error (from
    castbridge, ADR 0016). Cast session fields (ADR 0045): volume_control_type,
    volume_step_interval, app_id, content_type, stream_type — never content_id
    (may carry a debrid URL). Empty player_state when idle/unreachable. Never raises."""
    info = receiver_info(device)
    pos, dur, state = _cast_progress(info)
    if dur <= 0:
        dur = cast_delivery.live_duration(device) or dur  # live playlist (ADR 0039)
    title = (info.get("media_metadata") or {}).get("title") or info.get("title") or None
    # One `catt info` per status: an empty answer means unreachable/idle, and asking again
    # (the old `device_volume` retry) only doubled the wait — up to CATT_INFO_TIMEOUT more
    # on an unreachable TV, paid by every --status and --stop.
    vol, muted = _vol_muted(info)
    active_tracks, receiver_error = _bridge_track_info(device)
    return {
        "player_state": state or "IDLE",
        "title": title,
        "position": round(pos, 1) if pos else 0.0,
        "duration": round(dur, 1) if dur else 0.0,
        "volume": vol,
        "volume_percent": volume_level_to_percent(vol),
        "muted": muted,
        "volume_control_type": _opt_str(info, "volume_control_type"),
        "volume_step_interval": _opt_float(
            info, "volume_step_interval", "step_interval", "stepInterval"
        ),
        "app_id": _opt_str(info, "app_id"),
        "content_type": _opt_str(info, "content_type"),
        "stream_type": _opt_str(info, "stream_type"),
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


def _opt_str(info: dict, key: str) -> str | None:
    raw = info.get(key)
    if raw is None or raw == "":
        return None
    return str(raw)


def _opt_float(info: dict, *keys: str) -> float | None:
    for key in keys:
        raw = info.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


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
    """Set the receiver volume to Cast percent `level` (0–100) via `catt volume`.

    catt maps that integer to `SET_VOLUME` 0–1. On MASTER (`volume_step_interval`
    often null) the TV may quantize the readback (Phase 0: 14 → catt 13). Best-effort.
    """
    level = clamp_volume_percent(level)
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
