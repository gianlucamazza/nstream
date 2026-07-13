"""Shared low-level helpers: atomic writes, best-effort JSON loads, subprocess launches.

Stdlib-only with no internal dependencies (sits at the top of the import graph). Centralises
the three patterns that were reimplemented across modules — atomic file replace, JSON load
with a typed fallback, and external-command launches — so error handling and timeouts stay
consistent. All of it follows nstream's best-effort rule: disk/cache failures never block
playback (callers that must surface an error let `atomic_write` re-raise).
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import signal
import subprocess
import tempfile
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import IO

# External-command timeouts (seconds), centralised so they're consistent and tunable.
FFPROBE_TIMEOUT = 20.0
VAINFO_TIMEOUT = 10.0
CATT_SCAN_TIMEOUT = 20.0  # headroom for a cold mDNS scan on hosts with many interfaces
CATT_CAST_TIMEOUT = 30.0  # `catt cast` blocks while the receiver buffers the remote URL (~10s)
CATT_INFO_TIMEOUT = 10.0  # one castv2 round-trip: `catt info -j` / `stop` / `volume`

# HTTP retry tuning, shared by every retrying client (api addon fetch, native debrid).
_BACKOFF_BASE = 0.5
_RETRY_AFTER_CAP = 30.0


def backoff(attempt: int) -> float:
    """Exponential backoff with jitter (seconds) for a 0-based retry `attempt`."""
    return _BACKOFF_BASE * (2**attempt) + random.uniform(0.0, 0.3)


def retry_after(exc: urllib.error.HTTPError) -> float | None:
    """Seconds to wait from a `Retry-After` header (capped), or None if absent/non-numeric."""
    value = exc.headers.get("Retry-After") if exc.headers else None
    if value and value.isdigit():
        return min(float(value), _RETRY_AFTER_CAP)
    return None


def atomic_write(
    path: Path, write_fn: Callable[[IO[str]], object], *, prefix: str, mode: int = 0o600
) -> None:
    """Write `path` atomically: a uniquely-named temp file in the same dir is created with
    `mode`, populated by `write_fn(file)`, then `os.replace`d over the target (so concurrent
    writers and readers never see a partial file). The temp file is cleaned up and the
    OSError re-raised on failure — best-effort callers wrap this in `try/except OSError`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        os.chmod(tmp, mode)  # set perms before any content is written
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            write_fn(f)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_bytes(path: Path, data: bytes, *, prefix: str, mode: int = 0o600) -> None:
    """Binary sibling of `atomic_write` for cached blobs (e.g. poster images): write
    `data` to a temp file in the target dir, chmod, then `os.replace` over the target.
    Same best-effort contract — temp cleaned up and OSError re-raised on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_json[T](path: Path, fallback: T) -> T:
    """Parse JSON from `path`, returning `fallback` if the file is missing, unreadable,
    corrupt, or of a different top-level type than the fallback (e.g. a list where a dict
    was expected). Best-effort: never raises."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback
    return data if isinstance(data, type(fallback)) else fallback


class RunState:
    """Tiny JSON state file tracking a detached helper process across nstream runs
    (`$XDG_RUNTIME_DIR/nstream-<name>.json`, falling back to the system temp dir) —
    the persistence half of the runtime-state machinery shared by `remux` (detached
    serving catt / Range server) and `mirror` (detached mpv + sender), so a later
    `--stop`/GC can find what an earlier headless run left serving. Best-effort like
    all nstream disk I/O: `read` returns None on any problem, `write`/`clear` never
    raise."""

    def __init__(self, name: str) -> None:
        base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self.path = Path(base) / f"nstream-{name}.json"

    def read(self) -> dict | None:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            return json.loads(self.path.read_text())
        return None

    def write(self, data: dict) -> None:
        # O_NOFOLLOW + 0600: on the world-writable /tmp fallback a pre-planted symlink
        # must not redirect the write (same hardening as bridge's runtime-dir fallback).
        with contextlib.suppress(OSError):
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data))

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def pid_alive(pid: int | None) -> bool:
    """True when `pid` refers to a live process (signal-0 probe). Best-effort: any
    refusal (gone, not ours) counts as not-alive."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill_pid(pid: int | None, *, pgroup: bool = False) -> None:
    """Best-effort SIGTERM to `pid`; with `pgroup` the whole process group is signalled
    first (for `start_new_session` helpers like the detached serving catt, whose own
    children must die with it). The plain-kill always follows so a helper that did not
    become a group leader is still terminated."""
    if not pid:
        return
    if pgroup:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def run_cmd(
    args: list[str],
    *,
    timeout: float | None = None,
    input: str | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str] | None:
    """Run an external command, returning the CompletedProcess or None if it couldn't run
    to completion (binary missing, timed out, or other launch error). Centralises the
    `(OSError, subprocess.SubprocessError)` handling so every optional-tool call (ffprobe,
    vainfo, catt, fzf) degrades gracefully the same way. Not for long-lived processes
    (mpv) — those use subprocess.Popen directly."""
    try:
        return subprocess.run(args, capture_output=capture, text=True, input=input, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
