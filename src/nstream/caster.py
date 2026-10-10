"""Chromecast playback via `catt`: device resolution, launching the cast, polling its
status for resume/auto-advance, and the in-cast audio-language switch.

Menus are injected by the frontend (ADR 0037): `resolve_device(confirm=, picker=)` and
`cast(choose_lang=)`; this module never imports the fzf picker. cli calls `cast()`,
`resolve_device()` and `CastUnavailable`. catt is invoked with subprocess
directly (the poll loop needs returncode/stderr and a per-iteration process).
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import importlib.metadata
import ipaddress
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from . import (
    bridge,
    cast_delivery,
    discovery,
    languages,
    log,
    notices,
    serve,
    srt,
    ui,
    urlproxy,
    util,
)
from .config import Config

_log = log.get_logger("cast")


@dataclass(frozen=True)
class CastMeta:
    """Now-playing metadata for the receiver LOAD. castbridge sends the full block
    (Movie/TvShow + poster). The catt **≥0.13.2** library path (`play_media_url`)
    sends title + thumb/images + streamType + contentType in one LOAD (ADR 0050).
    The CLI fallback (`-l`, `--stream-type`) has no `--thumb`. Sourced from Cinemeta
    in `cli` / `headless`. Empty fields are omitted."""

    poster: str = ""
    subtitle: str = ""
    series_title: str = ""
    season: int = 0
    episode: int = 0
    content_type: str = ""


# catt ≥0.13.2 DefaultCastController.play_media_url → pychromecast play_media.
# 0.13.0/0.13.1: no `-l/--title`, drop media_info; 0.13.1 play_media_url can hang.
# Library: title/thumb/content_type/stream_type + media_info.metadata.metadataType.
# CLI: `-l/--title` and `--stream-type` only (no `--thumb`).
CATT_STREAM_BUFFERED = "BUFFERED"
CATT_METADATA_GENERIC = 0
CATT_METADATA_MOVIE = 1  # pychromecast METADATA_TYPE_MOVIE
CATT_METADATA_TVSHOW = 2  # pychromecast METADATA_TYPE_TVSHOW
CATT_LOAD_META_MIN = (0, 13, 2)
_SUB_IDLE_FOR = "_nstream_sub_idle_for"  # stashed on caption kwargs; never a LOAD field
_CATT_POSTER_HOST_SUFFIX = (".metahub.space", ".strem.io")
_CATT_POSTER_HOSTS = frozenset(
    {
        "metahub.space",
        "images.metahub.space",
        "live.metahub.space",
        "strem.io",
        "cinemeta.strem.io",
        "v3-cinemeta.strem.io",
        "images.strem.io",
    }
)


def catt_display_title(title: str, meta: CastMeta | None = None) -> str:
    """Single-line title for catt `-l` / play_url(title=). Series become
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


def catt_poster_url(poster: str) -> str:
    """Cinemeta / metahub HTTPS poster the TV fetches itself. Empty otherwise —
    never rewritten onto the remux/debrid host, never an arbitrary https URL."""
    raw = (poster or "").strip()
    if not raw.startswith("https://"):
        return ""
    host = (urllib.parse.urlsplit(raw).hostname or "").lower().rstrip(".")
    if not host:
        return ""
    if host in _CATT_POSTER_HOSTS or host.endswith(_CATT_POSTER_HOST_SUFFIX):
        return raw
    return ""


def _parse_catt_version(raw: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


@functools.cache
def catt_version() -> tuple[int, int, int] | None:
    """catt on PATH (CLI / helper shebang). None when it cannot be read."""
    try:
        proc = subprocess.run(
            ["catt", "--version"],
            capture_output=True,
            text=True,
            timeout=util.CATT_INFO_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None:
        parsed = _parse_catt_version(f"{proc.stdout or ''} {proc.stderr or ''}")
        if parsed:
            return parsed
    py = _catt_interpreter()
    if not py:
        return None
    try:
        probe = subprocess.run(
            [py, "-c", "import importlib.metadata as m; print(m.version('catt'))"],
            capture_output=True,
            text=True,
            timeout=util.CATT_INFO_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_catt_version(probe.stdout or "")


def catt_inprocess_version() -> tuple[int, int, int] | None:
    """catt imported in this env (`CattDevice`), not the binary on PATH."""
    try:
        mod = importlib.import_module("catt")
    except ImportError:
        return None
    parsed = _parse_catt_version(str(getattr(mod, "__version__", "") or ""))
    if parsed:
        return parsed
    with contextlib.suppress(importlib.metadata.PackageNotFoundError, ValueError):
        return _parse_catt_version(importlib.metadata.version("catt"))
    return None


def catt_supports_load_meta() -> bool:
    """PATH catt ≥0.13.2 (`-l`, `--stream-type`; helper play_media_url)."""
    ver = catt_version()
    return ver is not None and ver >= CATT_LOAD_META_MIN


def catt_inprocess_supports_load_meta() -> bool:
    """Imported catt ≥0.13.2 (`media_info`; timed play_media_url)."""
    ver = catt_inprocess_version()
    return ver is not None and ver >= CATT_LOAD_META_MIN


def catt_device_ctor_kwargs(device: str) -> dict[str, str]:
    """CattDevice ctor kwargs: `ip_addr=` for an IP, `name=` for a friendly name."""
    raw = (device or "").strip()
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return {"name": raw}
    return {"ip_addr": raw}


def catt_cast_argv(
    device: str | None,
    source: str,
    *,
    title: str = "",
    start: float | None = None,
    sub_path: str | None = None,
    stream_type: str = CATT_STREAM_BUFFERED,
) -> list[str]:
    """`catt cast` argv — CLI **fallback** when `catt.api` is not importable (ADR 0050).

    `-l` / `--stream-type` require catt ≥0.13.2; older click rejects them (cast_failed).
    No `--thumb`: the library path sends `thumb=` instead. Never put a debrid URL in
    logs — callers redact `source` themselves.
    """
    args = ["catt", *(["-d", device] if device else []), "cast", source]
    if start and start > 1:
        args += ["-t", str(int(start))]
    if sub_path:
        args += ["-s", sub_path]
    if not catt_supports_load_meta():
        return args
    label = " ".join(title.split()) if title else ""
    if label:
        args += ["-l", label]
    if stream_type:
        args += ["--stream-type", stream_type]
    return args


def _without_catt_meta_flags(argv: list[str]) -> list[str]:
    """Drop `-l` / `--title` / `--stream-type` and their values (old catt retry)."""
    out: list[str] = []
    skip = 0
    for item in argv:
        if skip:
            skip -= 1
            continue
        if item in ("-l", "--title", "--stream-type"):
            skip = 1
            continue
        out.append(item)
    return out


def _catt_cli_rejects_meta_flags(stderr: str) -> bool:
    text = (stderr or "").lower()
    return "no such option" in text and ("-l" in text or "title" in text or "stream-type" in text)


def _catt_cli_run(
    launch: list[str], *, retry_meta_flags: bool = True
) -> subprocess.CompletedProcess:
    """`catt cast`; retry once without ≥0.13.2 flags if click rejects them.

    Skip the retry after a library attempt so worst-case send stays
    lib + confirm + one CLI (ADR 0050), not lib + CLI + retry.
    """
    proc = subprocess.run(launch, capture_output=True, text=True, timeout=util.CATT_CAST_TIMEOUT)
    if (
        retry_meta_flags
        and proc.returncode != 0
        and _catt_cli_rejects_meta_flags(proc.stderr)
        and any(a in launch for a in ("-l", "--title", "--stream-type"))
    ):
        retry = _without_catt_meta_flags(launch)
        _log.warning("catt rejected -l/--stream-type → retry without meta flags")
        return subprocess.run(retry, capture_output=True, text=True, timeout=util.CATT_CAST_TIMEOUT)
    return proc


def catt_metadata_type(meta: CastMeta | None) -> int:
    """Movie (1) or TvShow (2). pychromecast `play_media(metadata=)` / `media_info`."""
    if meta and meta.series_title and meta.season > 0 and meta.episode > 0:
        return CATT_METADATA_TVSHOW
    return CATT_METADATA_MOVIE


def catt_play_kwargs(
    title: str,
    meta: CastMeta | None = None,
    *,
    content_type: str = "video/mp4",
    start: float | None = None,
    subtitle_url: str = "",
) -> dict:
    """kwargs for catt ≥0.13.2 `CattDevice.controller.play_media_url` (one LOAD).

    title, thumb (https poster), content_type, stream_type=BUFFERED, plus
    `media_info.metadata` with metadataType 1/2 **and** `images: [{url}]` when
    the poster is an allowlisted Cinemeta/metahub https URL. pychromecast 14.x
    `**media_info` replaces `media.metadata` before it copies `thumb=` into
    `images[]`; a metadata dict that only had `metadataType` left the LOAD
    without `images` whenever `thumb` was missing. Put images on the Movie
    block ourselves (ADR 0050).
    """
    meta = meta or CastMeta()
    mime = (meta.content_type or content_type or "video/mp4").strip() or "video/mp4"
    md = dict(catt_lib_media_info(title, meta, content_type=mime)["metadata"])
    out: dict = {
        "title": md.get("title") or catt_display_title(title, meta),
        "content_type": mime,
        "stream_type": CATT_STREAM_BUFFERED,
        "media_info": {"metadata": md},
    }
    poster = catt_poster_url(meta.poster)
    if poster:
        out["thumb"] = poster
    if start and start > 1:
        out["current_time"] = float(start)
    if subtitle_url:
        out["subtitle_url"] = subtitle_url
    return out


def catt_media_info(
    *,
    title: str = "",
    poster: str = "",
    content_type: str = "video/mp4",
    stream_type: str = CATT_STREAM_BUFFERED,
    content_id: str = "",
    metadata_type: int = CATT_METADATA_MOVIE,
    series_title: str = "",
    season: int = 0,
    episode: int = 0,
    subtitle: str = "",
) -> dict:
    """MediaInformation body catt ≥0.13.2 / pychromecast `play_media` puts on LOAD.

    Expected body (derived from pychromecast `_send_start_play_media`, not a field
    capture). `thumb=` becomes `metadata.thumb` + `images[0].url`. `metadataType`
    1/2 is cheap via `media_info`. `content_id` is the Cast `contentId`; tests pass
    a label, never a debrid URL.
    """
    metadata: dict = {"metadataType": int(metadata_type)}
    label = " ".join(title.split()) if title else ""
    if label:
        metadata["title"] = label
    if series_title and metadata_type == CATT_METADATA_TVSHOW:
        metadata["seriesTitle"] = series_title
        if season > 0:
            metadata["season"] = int(season)
        if episode > 0:
            metadata["episode"] = int(episode)
    if subtitle:
        metadata["subtitle"] = subtitle
    image = catt_poster_url(poster)
    if image:
        metadata["thumb"] = image
        metadata["images"] = [{"url": image}]
    media: dict = {
        "streamType": stream_type or CATT_STREAM_BUFFERED,
        "contentType": content_type or "video/mp4",
        "metadata": metadata,
    }
    if content_id:
        media["contentId"] = content_id
    return media


def catt_lib_media_info(
    title: str,
    meta: CastMeta | None = None,
    *,
    content_type: str = "video/mp4",
    content_id: str = "",
) -> dict:
    """LOAD MediaInformation the catt **library** path sends (title + images when https)."""
    meta = meta or CastMeta()
    return catt_media_info(
        title=catt_display_title(title, meta),
        poster=meta.poster,
        content_type=(meta.content_type or content_type or "video/mp4"),
        metadata_type=catt_metadata_type(meta),
        series_title=meta.series_title,
        season=meta.season,
        episode=meta.episode,
        subtitle=meta.subtitle,
        content_id=content_id,
    )


def catt_cli_media_info(
    title: str,
    meta: CastMeta | None = None,
    *,
    content_type: str = "video/mp4",
    stream_type: str = CATT_STREAM_BUFFERED,
    content_id: str = "",
) -> dict:
    """LOAD MediaInformation the catt **CLI** fallback can send (no poster)."""
    return catt_media_info(
        title=catt_display_title(title, meta),
        content_type=(meta.content_type if meta and meta.content_type else content_type),
        stream_type=stream_type,
        content_id=content_id,
        metadata_type=CATT_METADATA_GENERIC,
    )


def _catt_device_cls():
    """CattDevice if `catt` is importable in this env (not a declared nstream dep)."""
    try:
        return importlib.import_module("catt.api").CattDevice
    except ImportError:
        return None


@functools.cache
def _catt_interpreter() -> str | None:
    """Python that can `import catt.api` — shebang of `catt` on PATH, else None."""
    catt = shutil.which("catt")
    if not catt:
        return None
    py = ""
    try:
        with open(catt, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    if first.startswith("#!"):
        parts = first[2:].strip().split()
        if parts and Path(parts[0]).name == "env" and len(parts) >= 2:
            py = shutil.which(parts[-1]) or ""
        elif parts:
            py = parts[0]
    if not py:
        sibling = Path(catt).resolve().parent / "python"
        py = str(sibling) if sibling.is_file() else ""
    if not py:
        return None
    try:
        probe = subprocess.run(
            [py, "-c", "import catt.api"],
            capture_output=True,
            timeout=util.CATT_INFO_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return py if probe.returncode == 0 else None


def catt_can_lib_load() -> bool:
    """True when a catt ≥0.13.2 library LOAD is possible (in-process or helper)."""
    if _catt_device_cls() is not None:
        return catt_inprocess_supports_load_meta()
    return catt_supports_load_meta() and _catt_interpreter() is not None


CATT_LIB_OK = "ok"
CATT_LIB_FAIL = "fail"
CATT_LIB_UNCONFIRMED = "unconfirmed"  # LOAD sent; receiver not yet confirmed
_LIB_OK = CATT_LIB_OK
_LIB_FAIL = CATT_LIB_FAIL
_LIB_UNCONFIRMED = CATT_LIB_UNCONFIRMED
_CATT_RECEIVER_ACTIVE = frozenset({"PLAYING", "PAUSED", "BUFFERING"})
# catt 0.13.3 DefaultCastController.play_media_url after play_media() returns.
_CATT_SESSION_WAIT = "media session to become active timed out"
_CATT_IDLE_FAIL = frozenset({"ERROR", "LOAD_FAILED"})


def _same_cast_content(content_id: str, url: str) -> bool:
    """True if the receiver content_id is the URL we LOADed. Never logs either."""
    got, want = (content_id or "").strip(), (url or "").strip()
    if not got or not want:
        return False
    if got == want:
        return True
    a, b = urllib.parse.urlsplit(got), urllib.parse.urlsplit(want)
    return bool(a.path) and (a.path, a.query) == (b.path, b.query)


def catt_receiver_has_load(device: str | None, url: str) -> bool:
    """Short bounded check: receiver is playing/buffering the URL we LOADed.

    Used when the library/helper call timed out — catt's own PLAYING wait starts
    after the LOAD, so a deadline can fire while the TV already has the media.
    Never logs `url`.
    """
    return catt_receiver_load_state(device, url) == _LIB_OK


def catt_receiver_load_state(device: str | None, url: str) -> str:
    """Receiver vs our LOAD: `ok` / `failed` / `unconfirmed` (still pending).

    `failed` is an explicit refuse (LOAD_FAILED / idle_reason ERROR) for
    *our* content_id (or an empty one). A different content_id is leftover
    from an earlier session. INTERRUPTED is never a refuse. Never logs `url`.
    """
    info = receiver_info(device)
    if not info:
        return _LIB_UNCONFIRMED
    state = str(info.get("player_state") or "").upper()
    idle = str(info.get("idle_reason") or info.get("idleReason") or "").upper()
    err = str(
        info.get("error") or info.get("receiver_error") or info.get("last_error") or ""
    ).upper()
    content = str(info.get("content_id") or info.get("contentId") or "")
    blob = f"{state} {idle} {err}"
    refused = (
        state in _CATT_IDLE_FAIL
        or idle in _CATT_IDLE_FAIL
        or "LOAD_FAILED" in blob
        or (err in _CATT_IDLE_FAIL and state in {"IDLE", "UNKNOWN", ""})
    )
    if refused:
        # INTERRUPTED is a replaced session, not a refuse. ERROR/LOAD_FAILED
        # about a *different* content_id is leftover from an earlier cast.
        if not content or _same_cast_content(content, url):
            return _LIB_FAIL
        return _LIB_UNCONFIRMED
    if state in _CATT_RECEIVER_ACTIVE and _same_cast_content(content, url):
        return _LIB_OK
    return _LIB_UNCONFIRMED


def _is_catt_session_wait(exc: BaseException) -> bool:
    """catt 0.13.3 post-LOAD CastError from block_until_active, not prep_app."""
    return type(exc).__name__ == "CastError" and _CATT_SESSION_WAIT in str(exc)


def _hook_catt_play_media(controller, box: dict):
    """Wrap MediaController.play_media. Returns a restore() or None.

    `sent` is set only after play_media returns. `abandoned` skips a late
    send after nstream has already given up (and may have CLI-fallen-back).
    """
    inner = getattr(controller, "_controller", None)
    if inner is None:
        return None
    play = getattr(inner, "play_media", None)
    if not callable(play):
        return None

    def wrapped(*a, **k):
        if box.get("abandoned"):
            return None
        out = play(*a, **k)
        box["sent"] = True
        return out

    inner.play_media = wrapped

    def restore() -> None:
        inner.play_media = play

    return restore


def _catt_lib_finish(device: str, url: str, outcome: str) -> str:
    """Map a lib/helper outcome. UNCONFIRMED polls the receiver for a grace window.

    Match → ok. LOAD_FAILED / idle ERROR → fail (caller may CLI). Still unknown
    at the bound → unconfirmed: no second CLI LOAD (keep metadata).
    """
    if outcome == _LIB_OK:
        return _LIB_OK
    if outcome != _LIB_UNCONFIRMED:
        return _LIB_FAIL
    deadline = time.monotonic() + util.CATT_LIB_CONFIRM_GRACE
    while True:
        state = catt_receiver_load_state(device, url)
        if state == _LIB_OK:
            _log.debug("catt lib LOAD unconfirmed → receiver already has media")
            return _LIB_OK
        if state == _LIB_FAIL:
            _log.warning("catt lib LOAD refused (LOAD_FAILED)")
            return _LIB_FAIL
        leftover = deadline - time.monotonic()
        if leftover <= 0:
            _log.info("catt sender=lib unconfirmed")
            return _LIB_UNCONFIRMED
        time.sleep(min(util.CATT_LIB_CONFIRM_POLL, leftover))


def _catt_play_media_kwargs(load: dict) -> dict:
    kwargs = {k: v for k, v in load.items() if k != "subtitle_url"}
    if load.get("subtitle_url"):
        kwargs["subtitles"] = load["subtitle_url"]
    return kwargs


def _catt_inprocess_play(Device, ident: dict[str, str], url: str, load: dict) -> str:
    """play_media_url on a worker thread. Sent only after play_media returns."""
    box: dict[str, object] = {"ok": False, "err": "", "sent": False, "abandoned": False}
    kwargs = _catt_play_media_kwargs(load)

    def run() -> None:
        restore = None
        try:
            dev = Device(**ident)
            ctrl = dev.controller
            restore = _hook_catt_play_media(ctrl, box)
            ctrl.prep_app()
            ctrl.play_media_url(url, **kwargs)
            box["ok"] = True
        except Exception as exc:  # catt/pychromecast: device missing, session wait, …
            box["err"] = type(exc).__name__
            if _is_catt_session_wait(exc):
                box["sent"] = True
        finally:
            if restore is not None:
                restore()

    worker = threading.Thread(target=run, name="nstream-catt-lib", daemon=True)
    worker.start()
    worker.join(util.CATT_LIB_LOAD_TIMEOUT)
    if worker.is_alive():
        box["abandoned"] = True
        _log.warning("catt lib LOAD timed out")
        return _LIB_UNCONFIRMED if box["sent"] else _LIB_FAIL
    if box["ok"]:
        return _LIB_OK
    if box["sent"]:
        _log.warning("catt lib LOAD unconfirmed after play_media: %s", box["err"] or "unknown")
        return _LIB_UNCONFIRMED
    _log.warning("catt lib LOAD failed: %s", box["err"] or "unknown")
    return _LIB_FAIL


def catt_lib_outcome(
    device: str,
    url: str,
    *,
    title: str = "",
    meta: CastMeta | None = None,
    start: float | None = None,
    content_type: str = "video/mp4",
    subtitle_url: str = "",
) -> str:
    """One LOAD via catt.api. Returns `ok` / `fail` / `unconfirmed`. Never logs `url`.

    `ok`: receiver is playing/buffering our content.
    `fail`: LOAD never sent, or the TV refused it (LOAD_FAILED) — caller may CLI.
    `unconfirmed`: LOAD went out; grace poll did not confirm or refuse. No CLI
    (a second LOAD would wipe metadataType 1 / images).
    """
    load = catt_play_kwargs(
        title, meta, content_type=content_type, start=start, subtitle_url=subtitle_url
    )
    ident = catt_device_ctor_kwargs(device)
    Device = _catt_device_cls()
    if Device is not None:
        if not catt_inprocess_supports_load_meta():
            return _LIB_FAIL
        return _catt_lib_finish(device, url, _catt_inprocess_play(Device, ident, url, load))
    if not catt_supports_load_meta():
        return _LIB_FAIL
    py = _catt_interpreter()
    helper = Path(__file__).with_name("_catt_load.py")
    if not py or not helper.is_file():
        return _LIB_FAIL
    payload = {"url": url, **load}
    if "ip_addr" in ident:
        payload["ip"] = ident["ip_addr"]
    else:
        payload["name"] = ident["name"]
    try:
        proc = subprocess.run(
            [py, os.fspath(helper)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=util.CATT_LIB_LOAD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _log.warning("catt helper LOAD timed out")
        return _catt_lib_finish(device, url, _LIB_UNCONFIRMED)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("catt helper LOAD failed: %s", type(exc).__name__)
        return _LIB_FAIL
    if proc.returncode == 0:
        return _LIB_OK
    if proc.returncode == 4:
        _log.warning("catt helper LOAD rc=4 (session wait after LOAD)")
        return _catt_lib_finish(device, url, _LIB_UNCONFIRMED)
    _log.warning("catt helper LOAD rc=%s (never sent)", proc.returncode)
    return _LIB_FAIL


def catt_lib_play(
    device: str,
    url: str,
    *,
    title: str = "",
    meta: CastMeta | None = None,
    start: float | None = None,
    content_type: str = "video/mp4",
    subtitle_url: str = "",
) -> bool:
    """True when the library LOAD is confirmed on the receiver (`catt_lib_outcome` is ok)."""
    return (
        catt_lib_outcome(
            device,
            url,
            title=title,
            meta=meta,
            start=start,
            content_type=content_type,
            subtitle_url=subtitle_url,
        )
        == _LIB_OK
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
    *,
    device: str | None = None,
    title: str = "",
    meta: CastMeta | None = None,
) -> None:
    """Re-cast a release in the chosen audio language from the current position.
    The Chromecast plays the file's default track, so this picks a differently-dubbed
    release rather than switching tracks in place (best-effort, single-dub friendly).
    Prefers the same library/helper LOAD as the first cast so title/thumb survive."""
    lang = choose_lang(langs)
    if lang is None:  # ESC → keep the current cast
        return
    print(f"{ui.g().tv} cambio audio: {languages.name(lang)}…", file=sys.stderr)
    new = resolve_lang(lang)
    if not new:
        notices.emit(f"nessuno stream {lang} compatibile col Chromecast")
        return
    print(f"{ui.g().tv} preparo il cast su {dest}…", file=sys.stderr)
    target = device or (None if dest == "Chromecast" else dest)
    if target:
        switched = catt_lib_outcome(target, new, title=title, meta=meta, start=pos)
        if switched == CATT_LIB_OK:
            return
        if switched == CATT_LIB_UNCONFIRMED:
            _catt_unconfirmed_notice()
            return
    launch = catt_cast_argv(target, new, title=catt_display_title(title, meta), start=pos)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            launch or [*base, "cast", new, "-t", str(int(pos))],
            capture_output=True,
            text=True,
            timeout=util.CATT_CAST_TIMEOUT,
        )


@dataclass(frozen=True)
class LanMedia:
    """A remote stream re-homed on the LAN Range server (ADR 0045 Phase 1)."""

    url: str
    content_type: str
    shutdown: Callable[[], None] | None
    plan: urlproxy.LanPlan
    idle_for: Callable[[], float] | None = None


def lan_media(
    cfg: Config,
    url: str,
    device: str | None,
    *,
    container: str = "",
    video_codec: str = "",
    follow: bool = True,
) -> LanMedia | None:
    """Range-serve `url` on the host LAN so the TV does not pull a remote debrid host.

    None when the config gate is off, the url is already local, `device` is missing
    (fail closed — never bind `0.0.0.0` / LOAD `http://0.0.0.0`), the plan is not
    `proxy` (rewrap / no-Range / unknown length / probe fail), or the detached
    server cannot start. Never logs `url`."""
    if not cfg.cast_lan_proxy or not urlproxy.is_remote(url):
        return None
    if not device:
        _log.warning("lan-proxy: device assente → bind rifiutato")
        return None
    from . import tracks

    probed = urlproxy.probe(url)
    tr = tracks.probe_tracks(url)
    # Probed codec wins when ffprobe ran (ADR 0051); claimed is only the fallback.
    planned = urlproxy.plan(container, probed, tr.video_codec or video_codec, tr.codec_tag)
    if planned.mode != "proxy":
        return None
    bind_ip = serve.lan_ip(device)
    serve.ensure_firewall(bind_ip)
    serve.reap_proxy_server()
    if follow:
        server, port, _thread = serve.serve_file(
            None, bind_ip,
            upstream=url, upstream_type=planned.content_type,
            upstream_length=planned.content_length, upstream_ranged=planned.ranged,
            media_name=planned.media_name,
        )  # fmt: skip
        return LanMedia(
            serve.served_url(bind_ip, port, server.token, planned.media_name),
            planned.content_type,
            lambda: serve.close_server(server),
            planned,
            idle_for=server.idle_for,
        )
    spawned = serve.spawn_detached(bind_ip, proxy=urlproxy.proxy_job(url, planned))
    if spawned is None:
        _log.warning("lan-proxy: detach fallito")
        return None
    pid, port, token = spawned
    serve.register_proxy_server(pid)
    return LanMedia(
        serve.served_url(bind_ip, port, token, planned.media_name),
        planned.content_type,
        None,
        planned,
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
    container: str = "",
    video_codec: str = "",
) -> cast_delivery.CastResult:
    """Cast `url` to a Chromecast and track playback so resume and series auto-advance work like
    the mpv path. Returns a `CastResult` — read it by attribute (ADR 0031). The advance decision
    belongs to `cast_flow`, not to a delivery backend (ADR 0029).     Prefers the **castbridge**
    native sender (Movie/TvShow LOAD + poster + a real event stream) when its binary is
    available; falls back to **catt ≥0.13.2 library** `play_media_url` (title +
    Cinemeta/metahub thumb + BUFFERED in one LOAD, ADR 0050), then the catt **CLI**
    (`-l` + `--stream-type`; no `--thumb`) when catt.api is not importable. The
    in-cast audio switch ('a') reuses that same library/helper path.
    `on_event` receives normalized events for the headless `--follow` JSONL path.

    A remote http(s) url is Range-served from the host LAN first (ADR 0045 Phase 1) so the
    TV does not pull a debrid host; `CastResult.delivery` is then `"lan"`. `container` /
    `video_codec` feed the proxy-vs-rewrap decision (`quality.CAST_*`). With
    `cast_lan_proxy` on, a non-proxyable plan (no Range, unknown length, rewrap, missing
    `device`) fails closed — never a silent WAN-direct of the debrid url.

    `subs_delivered`: whether the requested `sub_paths` were actually attached to the cast. Both
    senders now carry subtitles: the castbridge path serves the SRT as a side-loaded WebVTT track
    (`sub_lang` labels it), the catt path uses `-s`."""
    # The in-cast switch needs a frontend menu (`choose_lang`, ADR 0037); stdin being a TTY
    # is only the capability to read the 'a' keypress (`_poll_wait`), not the policy.
    lan = lan_media(cfg, url, device, container=container, video_codec=video_codec, follow=follow)
    if lan is not None:
        url = lan.url
        meta = replace(meta or CastMeta(), content_type=lan.content_type)
    elif cfg.cast_lan_proxy and urlproxy.is_remote(url):
        # Fail closed: never hand the debrid URL to the TV over the WAN.
        reason = "lan_no_bind" if not device else _lan_fail_reason(url, container, video_codec)
        _log.warning("lan-proxy: rifiuto WAN-direct (%s)", reason)
        return cast_delivery.CastResult(0.0, 0.0, error=reason)
    lan_url = lan.url if lan is not None else ""
    result: cast_delivery.CastResult | None = None
    try:
        result = _cast_senders(
            cfg, title, url, device=device, start=start, sub_paths=sub_paths,
            sub_lang=sub_lang, langs=langs, resolve_lang=resolve_lang,
            choose_lang=choose_lang, follow=follow, meta=meta, on_event=on_event,
            delivery="lan" if lan is not None else "",
        )  # fmt: skip
        return result
    finally:
        # ADR 0050: a late library LOAD can time out while the TV already has
        # our content_id (the LAN capability path). Do not tear the server down.
        if lan is not None and lan.shutdown is not None:
            # Follow already observed playback then IDLE: skip a final `catt info`.
            if follow and result is not None and result.started:
                lan.shutdown()
            elif result is not None and result.unconfirmed:
                # LOAD sent, not confirmed: keep this server only; idle-reap it.
                shutdown = lan.shutdown
                served = lan_url
                dest = device
                serve.register_inproc_proxy(shutdown)
                serve.schedule_reap(
                    shutdown,
                    util.CATT_LIB_UNCONFIRMED_SERVE_S,
                    handle=shutdown,
                    skip_if=lambda: bool(dest and served and catt_receiver_has_load(dest, served)),
                    idle_for=lan.idle_for,
                )
            elif device and lan_url and catt_receiver_has_load(device, lan_url):
                _log.debug("lan-proxy: LOAD unconfirmed, receiver already has media")
                serve.register_inproc_proxy(lan.shutdown)
            else:
                lan.shutdown()


def _lan_fail_reason(url: str, container: str, video_codec: str) -> str:
    from . import tracks

    probed = urlproxy.probe(url)
    tr = tracks.probe_tracks(url)
    planned = urlproxy.plan(container, probed, tr.video_codec or video_codec, tr.codec_tag)
    return planned.reason if planned.mode != "proxy" else "lan_unavailable"


def _cast_senders(
    cfg: Config,
    title: str,
    url: str,
    *,
    device: str | None,
    start: float | None,
    sub_paths: tuple[str, ...],
    sub_lang: str | None,
    langs: tuple[str, ...],
    resolve_lang: Callable[[str], str | None] | None,
    choose_lang: ChooseLang | None,
    follow: bool,
    meta: CastMeta | None,
    on_event: EventCb | None,
    delivery: str,
) -> cast_delivery.CastResult:
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
            return result._replace(delivery=delivery or result.delivery)
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
    return catt_result._replace(subs_delivered=bool(sub_paths), delivery=delivery)


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
    kwargs.pop(_SUB_IDLE_FOR, None)

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
        kwargs[_SUB_IDLE_FOR] = server.idle_for
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


def _catt_unconfirmed_notice() -> None:
    """Honest unconfirmed LOAD: UI warning, no new --json fields (ADR 0050)."""
    notices.emit(
        "cast avviato ma non confermato — il TV potrebbe ancora partire",
        code="cast_unconfirmed",
    )


def _catt_unconfirmed_result() -> cast_delivery.CastResult:
    """Reuse `cast_never_started`: LOAD sent, playback not observed. `ok` stays false."""
    return cast_delivery.CastResult(
        0.0, 0.0, started=False, error="cast_never_started", unconfirmed=True
    )


def _schedule_unconfirmed_sub_reap(shutdown, device: str | None, url: str, idle_for=None) -> None:
    """Keep an unconfirmed subtitle server; skip_if once at fire, same as remux/LAN."""
    dest, served = device, url
    serve.schedule_reap(
        shutdown,
        util.CATT_LIB_UNCONFIRMED_SERVE_S,
        handle=shutdown,
        skip_if=lambda: bool(dest and served and catt_receiver_has_load(dest, served)),
        idle_for=idle_for,
    )


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

    Prefers catt ≥0.13.2 **library** `play_media_url` (title + Cinemeta/metahub thumb
    + BUFFERED in one LOAD, ADR 0050) for remux, LAN Range-proxy, hev1-rewrap, and
    direct LAN URLs. CLI fallback (`-l` + `--stream-type`; no `--thumb`) only when
    `catt_can_lib_load()` is false, the library call raises before a LOAD, or the
    TV refuses the LOAD (LOAD_FAILED). A post-LOAD session wait that never
    confirms is `CastResult(started=False, error=cast_never_started)` — no second
    CLI LOAD, notice `cast_unconfirmed`.
    Older catt (0.13.0/0.13.1) keeps the pre-0050 argv so click does not reject `-l`.

    `follow=False` (headless fire-and-return): once the LOAD has handed the media to
    the receiver, return immediately without the resume poll loop — so an agent isn't
    held for the whole runtime. No position is tracked (no resume) in that mode."""
    base = ["catt", *(["-d", device] if device else [])]
    dest = device or "Chromecast"
    loaded = False
    lib_attempted = False
    lib_unconfirmed = False
    sub_shutdown = None
    sub_idle_for = None
    if device and catt_can_lib_load():
        cap: dict = {}
        if sub_paths:
            vtt = srt.to_vtt(sub_paths[0]) or None
            sub_shutdown = _serve_subtitle(vtt, device, None, follow, cap)
            sub_idle_for = cap.pop(_SUB_IDLE_FOR, None)
        ui.status(f"consegno a {dest}…", kind="tv")
        lib_attempted = True
        try:
            outcome = catt_lib_outcome(
                device,
                url,
                title=title,
                meta=meta,
                start=start,
                content_type=(meta.content_type if meta and meta.content_type else ""),
                subtitle_url=str(cap.get("subtitle_url") or ""),
            )
        except Exception as exc:  # lib path raised before a LOAD — CLI is the fallback
            _log.warning("catt lib LOAD raised: %s → CLI fallback", type(exc).__name__)
            outcome = CATT_LIB_FAIL
            lib_attempted = False
        if outcome == CATT_LIB_OK:
            loaded = True
            _log.info("catt sender=lib")
        elif outcome == CATT_LIB_UNCONFIRMED:
            _log.info("catt sender=lib unconfirmed")
            _catt_unconfirmed_notice()
            lib_unconfirmed = True
            if not follow:
                # Keep the subtitle server for a late TV fetch; idle-reap this handle.
                if sub_shutdown is not None:
                    _schedule_unconfirmed_sub_reap(sub_shutdown, device, url, idle_for=sub_idle_for)
                _emit(on_event, "failed", error="cast_never_started", message="cast non confermato")
                return _catt_unconfirmed_result()
            loaded = True  # follow: poll; do not CLI-overwrite metadata
        else:
            _log.info("catt lib LOAD never sent → CLI fallback")
            if sub_shutdown is not None:
                sub_shutdown()
                sub_shutdown = None
            serve.reap_sub_server()
    if not loaded:
        _log.info("catt sender=cli")
        launch = catt_cast_argv(
            device,
            url,
            title=catt_display_title(title, meta),
            start=start,
            sub_path=catt_sub(sub_paths[0]) if sub_paths else None,
        )
        # Never log the URL itself: the redaction regexes cover the known token carriers, but a
        # signed native-CDN link (TorBox/Premiumize requestdl) is a capability in its own right
        # and its query params don't necessarily match them.
        _log.debug("catt launch: %s", " ".join(a if a != url else "<url>" for a in launch))
        ui.status(f"consegno a {dest}…", kind="tv")
        try:
            proc = _catt_cli_run(launch, retry_meta_flags=not lib_attempted)
        except FileNotFoundError:
            notices.emit("catt non trovato")
            _emit(on_event, "failed", error="catt_missing", message="catt non trovato")
            return cast_delivery.CastResult(0.0, 0.0, error="catt_missing")
        except subprocess.TimeoutExpired:
            _log.warning("catt cast bloccato oltre %.0fs → annullato", util.CATT_CAST_TIMEOUT)
            notices.emit("cast non riuscito (timeout)")
            _emit(on_event, "failed", error="cast_timeout", message="cast non riuscito (timeout)")
            return cast_delivery.CastResult(0.0, 0.0, error="cast_timeout")
        if proc.returncode != 0:
            _log.warning(
                "cast non riuscito (rc=%s): %s", proc.returncode, proc.stderr.strip()[:300]
            )
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
        if sub_shutdown is not None:
            sub_shutdown()
        return cast_delivery.CastResult(0.0, 0.0, started=True)

    holder = {"position": 0.0, "duration": 0.0}
    started = False
    warned_vol = False
    idle = 0  # consecutive polls without progress before playback ever starts
    try:
        while True:
            if _poll_wait(_CAST_POLL) == "a" and can_switch:
                _switch_cast_audio(
                    base, langs, resolve_lang, choose_lang, holder["position"], dest,
                    device=device, title=title, meta=meta,
                )  # fmt: skip
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
    if sub_shutdown is not None:
        if lib_unconfirmed and not started:
            _schedule_unconfirmed_sub_reap(sub_shutdown, device, url, idle_for=sub_idle_for)
        else:
            sub_shutdown()
    return cast_delivery.CastResult(
        holder["position"], holder["duration"],
        started=started, error=None if started else "cast_never_started",
        unconfirmed=lib_unconfirmed and not started,
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
    """catt launches the Default Media Receiver only. A configured custom id is a
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
