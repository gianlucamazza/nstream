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
import subprocess
import tempfile
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import IO

# External-command timeouts (seconds), centralised so they're consistent and tunable.
FFPROBE_TIMEOUT = 20.0
VAINFO_TIMEOUT = 10.0
CATT_SCAN_TIMEOUT = 15.0

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
