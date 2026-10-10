"""Shared low-level helpers: atomic writes, best-effort JSON loads, subprocess launches.

Stdlib-only with no internal dependencies (sits at the top of the import graph). Centralises
the three patterns that were reimplemented across modules — atomic file replace, JSON load
with a typed fallback, and external-command launches — so error handling and timeouts stay
consistent. All of it follows nstream's best-effort rule: disk/cache failures never block
playback (callers that must surface an error let `atomic_write` re-raise).
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import json
import os
import random
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import IO, cast


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 0.1):
    """Serialize updates without making playback wait indefinitely for another process."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        path.with_name(f".{path.stem}.lock"), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("state lock busy") from None
                time.sleep(0.005)
        yield
    finally:
        os.close(fd)


def state_update(path_fn: Callable[[], Path], fallback=None):
    """Best-effort locked read/modify/write; never run an update without the lock."""

    def decorate[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def update(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                path = path_fn()
                with file_lock(path):
                    if path.is_symlink():
                        return cast(R, fallback)
                    return fn(*args, **kwargs)
            except OSError:
                return cast(R, fallback)

        return update

    return decorate


# External-command timeouts (seconds), centralised so they're consistent and tunable.
FFPROBE_TIMEOUT = 20.0
VAINFO_TIMEOUT = 10.0
CATT_SCAN_TIMEOUT = 20.0  # headroom for a cold mDNS scan on hosts with many interfaces
CATT_CAST_TIMEOUT = 30.0  # `catt cast` blocks while the receiver buffers the remote URL (~10s)
CATT_LIB_LOAD_TIMEOUT = 55.0  # connect + prep_app + catt's own 30s PLAYING wait (ADR 0050)
CATT_LIB_CONFIRM_GRACE = 20.0  # HEVC slow start after a post-LOAD session-wait miss
CATT_LIB_CONFIRM_POLL = 1.0
CATT_LIB_UNCONFIRMED_SERVE_S = 15 * 60.0  # bound a leftover remux/LAN server (ADR 0050)
CATT_INFO_TIMEOUT = 10.0  # one castv2 round-trip: `catt info -j` / `stop` / `volume`
# Worst-case catt send (ADR 0050): library 55s + grace 20s + one CLI 30s (only when
# no LOAD was sent) ≈ 105s. Click-flag retry is CLI-only, 30+30=60s.

# HTTP retry tuning, shared by every retrying client (api addon fetch, native debrid).
_BACKOFF_BASE = 0.5
_RETRY_AFTER_CAP = 30.0


def backoff(attempt: int) -> float:
    """Exponential backoff with jitter (seconds) for a 0-based retry `attempt`."""
    return _BACKOFF_BASE * (2**attempt) + random.uniform(0.0, 0.3)


# Container extensions stripped from a release key: the same release is published with and
# without the extension depending on which field carries it (`behaviorHints.filename` keeps
# it, a `description`/`title` headline usually doesn't).
_RELEASE_EXT_RE = re.compile(r"\.(mkv|mp4|m4v|mov|avi|webm|wmv|ts)$", re.I)


def release_key(name: str) -> str:
    """Normalized join key identifying one release across addons and query variants (ADR 0026).

    Case-folded with the container extension stripped — the two differences that are pure
    formatting. Separator style is deliberately NOT normalized: `A.Film.2024.WEBRip` and
    `A Film 2024 WEBRip` are usually different releases, and collapsing them would merge
    distinct rows rather than deduplicate one.
    """
    return _RELEASE_EXT_RE.sub("", name.strip()).casefold()


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
    # Preserve malformed JSON only when an actual overwrite is about to occur.
    if path.suffix == ".json" and path.exists() and not path.is_symlink():
        raw = path.read_bytes()
        try:
            if not isinstance(json.loads(raw), dict):
                raise ValueError("state must be a JSON object")
        except (ValueError, UnicodeError):
            backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
            atomic_write_bytes(backup, raw, prefix=".recovery-")
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        os.chmod(tmp, mode)  # set perms before any content is written
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
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


def fold_text(value: object) -> str:
    """Casefolded text with the accents removed (NFKD, combining marks dropped): "Léon"
    and "Leon" compare equal. The base of every title comparison."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(c for c in text if not unicodedata.combining(c))


def compact_text(value: object) -> str:
    """`fold_text` reduced to its letters and digits: separators and punctuation gone, so
    "I.S.S." is "iss" and "Spider-Man" is "spiderman"."""
    return "".join(c for c in fold_text(value) if c.isalnum())


def prune_lru(cache_dir: Path, *, max_files: int, max_bytes: int) -> None:
    """Best-effort: evict the oldest files (by mtime) of a flat cache dir until it is back
    under `max_files` / `max_bytes`. Errors are swallowed — a cache never costs its user."""
    entries = []
    with contextlib.suppress(OSError):
        for p in cache_dir.iterdir():
            with contextlib.suppress(OSError):
                if p.is_file():
                    entries.append((p, p.stat()))
    count = len(entries)
    total = sum(st.st_size for _, st in entries)
    entries.sort(key=lambda e: e[1].st_mtime)  # oldest first
    for p, st in entries:
        if count <= max_files and total <= max_bytes:
            break
        with contextlib.suppress(OSError):
            p.unlink()
            count -= 1
            total -= st.st_size


def load_json[T](path: Path, fallback: T) -> T:
    """Parse JSON from `path`, returning `fallback` if the file is missing, unreadable,
    corrupt, or of a different top-level type than the fallback (e.g. a list where a dict
    was expected). Best-effort: never raises."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return fallback
    return data if isinstance(data, type(fallback)) else fallback


class BoundedMemo[K, V]:
    """Thread-safe in-process memo with an LRU bound and optional expiry — for caches that
    used to be plain dicts "bounded by one run", which a long TUI session is not.

    `ttl` expires every entry after N seconds; `ttl_for(value)` may return a shorter
    per-value TTL (e.g. failed probes), None = the default. Supports the dict subset the
    callers use: get / [] / []= / in / clear / len."""

    def __init__(
        self,
        maxsize: int,
        *,
        ttl: float | None = None,
        ttl_for: Callable[[V], float | None] | None = None,
    ) -> None:
        from collections import OrderedDict

        self._data: OrderedDict[K, tuple[V, float]] = OrderedDict()
        self._lock = threading.Lock()
        self.maxsize, self.ttl, self.ttl_for = maxsize, ttl, ttl_for

    def _expiry(self, value: V) -> float:
        ttl = self.ttl_for(value) if self.ttl_for else None
        ttl = ttl if ttl is not None else self.ttl
        return time.monotonic() + ttl if ttl is not None else float("inf")

    def get(self, key: K, default: V | None = None) -> V | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return default
            if hit[1] <= time.monotonic():
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return hit[0]

    def __getitem__(self, key: K) -> V:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: K, value: V) -> None:
        with self._lock:
            self._data[key] = (value, self._expiry(value))
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def __contains__(self, key: object) -> bool:
        return self.get(key) is not None  # type: ignore[arg-type]

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def runtime_dir() -> Path:
    """Per-user runtime directory (`$XDG_RUNTIME_DIR`, tmpfs, per boot), falling back to the
    system temp dir — where per-play work dirs and detached-helper state live."""
    return Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())


class RunState:
    """Tiny JSON state file tracking a detached helper process across nstream runs
    (`$XDG_RUNTIME_DIR/nstream-<name>.json`, falling back to the system temp dir) —
    the persistence half of the runtime-state machinery shared by `remux` (detached
    serving catt / Range server) and `mirror` (detached mpv + sender), so a later
    `--stop`/GC can find what an earlier headless run left serving. Best-effort like
    all nstream disk I/O: `read` returns None on any problem, `write`/`clear` never
    raise."""

    def __init__(self, name: str) -> None:
        self.path = runtime_dir() / f"nstream-{name}.json"

    def read(self) -> dict | None:
        """The stored state, with every recorded pid (`pid` / `*_pid`) whose process start
        time no longer matches set to None: a reused pid belongs to an unrelated process,
        and callers SIGTERM what they read here."""
        with contextlib.suppress(OSError, ValueError, UnicodeError):
            if self.path.is_symlink() or self.path.stat().st_uid != os.getuid():
                return None
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                return None
            for key in _pid_keys(data):
                start = data.pop(f"{key}_start", None)
                if start is not None and proc_start(data[key]) != start:
                    data[key] = None
            return data
        return None

    def write(self, data: dict) -> None:
        # Private atomic replacement never follows a pre-planted destination symlink.
        data = dict(data)
        for key in _pid_keys(data):
            data[f"{key}_start"] = proc_start(data[key])
        with contextlib.suppress(OSError):
            if self.path.is_symlink():
                return
            atomic_write(self.path, lambda f: json.dump(data, f), prefix=".runtime-")

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def _pid_keys(data: dict) -> list[str]:
    return [
        k for k, v in data.items()
        if (k == "pid" or k.endswith("_pid")) and isinstance(v, int) and not isinstance(v, bool)
    ]  # fmt: skip


def proc_start(pid: int | None) -> str | None:
    """Kernel start time of `pid` (`/proc/<pid>/stat` field 22), or None when it isn't
    running or /proc is unavailable. (pid, start) identifies a process across pid reuse."""
    if not pid:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2 :].split()  # comm may contain spaces/parens
    return fields[19] if len(fields) > 19 else None


def free_gib(path: Path) -> float:
    """Free space on the filesystem holding `path` (or its nearest existing parent), in
    GiB — the scale release sizes are parsed in. 0.0 = couldn't be determined."""
    for p in (path, *path.parents):
        try:
            return shutil.disk_usage(p).free / 1024**3
        except FileNotFoundError:
            continue
        except OSError:
            return 0.0
    return 0.0


def die_with_parent() -> None:
    """`preexec_fn` for a child that must not outlive nstream (Linux PR_SET_PDEATHSIG):
    a headless run killed by an agent's timeout would otherwise leave ffmpeg fetching tens
    of GB. Best-effort no-op where prctl is unavailable. Only for children awaited by the
    thread that spawns them — the signal fires when that thread exits."""
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


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
