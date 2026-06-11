"""Background Chromecast discovery with an on-disk device cache.

`catt scan` takes up to ~2×20s on a cold mDNS network, which used to freeze the TUI at
cast time. This module makes discovery non-blocking: a daemon thread started at TUI
startup runs the scan while the user browses, and a TTL disk cache
(`$XDG_CACHE_HOME/nstream/devices.json`) plus a ~1s TCP probe (`verify`) make a known
device usable instantly across sessions. Leaf module (imports only `log`/`util` +
stdlib); callers own all user-facing messaging — a background scan must never print
over the fzf screen.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import threading
import time
from pathlib import Path

from . import log, util

_log = log.get_logger("discovery")

Device = tuple[str, str]  # (name, ip)

_CAST_PORT = 8009  # Chromecast control port (castv2 TLS): answers whenever the device is on
VERIFY_TIMEOUT = 1.0
# DHCP can reassign IPs, so a cache entry is a hint, not a fact — every actual use is
# gated behind verify(); the TTL only bounds how long a hint is worth probing at all.
CACHE_TTL = 24 * 3600.0

# `catt scan` text line: "192.0.2.10 - 43PUS9235/12 - Philips TPM191E". We parse
# IP + name from text because `catt scan -j` is broken in current catt (CastInfo has
# no _asdict). Shared by the background worker and the settings device picker.
_SCAN_RE = re.compile(r"^([\d.]+) - (.+?) - ")


def scan_sync(*, attempts: int = 2) -> list[Device]:
    """Discover Chromecasts on the LAN via `catt scan` as (name, ip) pairs (deduped by
    name, stable order). The IP lets callers cast with `catt -d <ip>`, which is robust
    to mDNS name-resolution flakiness (e.g. right after a network change). Best-effort:
    returns [] if catt is missing or every scan comes back empty.

    mDNS discovery is probabilistic and degrades on hosts with many interfaces (docker
    bridges, VPNs, veth): a single cold `catt scan` can return empty even when the device
    is reachable. A *populated* scan is trustworthy and returned immediately; only an empty
    result is retried (up to `attempts`) before we trust the absence and fall back to local."""
    for attempt in range(1, attempts + 1):
        devices = _scan_once()
        if devices:
            return devices
        if attempt < attempts:
            _log.debug("catt scan vuoto (tentativo %d/%d) → riprovo", attempt, attempts)
    return []


def _scan_once() -> list[Device]:
    """One `catt scan`, parsed to deduped (name, ip) pairs; [] if catt is missing/failed."""
    proc = util.run_cmd(["catt", "scan"], timeout=util.CATT_SCAN_TIMEOUT)
    if proc is None:
        return []
    devices: list[Device] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        m = _SCAN_RE.match(line.strip())
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            devices.append((m.group(2), m.group(1)))  # (name, ip)
    return devices


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "nstream" / "devices.json"


def load_cache() -> list[Device]:
    """Devices from the disk cache, or [] when missing/corrupt/stale. Entries may be
    outdated (TV off, IP reassigned) — gate any actual use behind `verify()`."""
    data = util.load_json(_cache_path(), {})
    ts = data.get("ts")
    devices = data.get("devices")
    if not (isinstance(ts, int | float) and time.time() - ts < CACHE_TTL):
        return []
    if not isinstance(devices, list):
        return []
    return [
        (entry[0], entry[1])
        for entry in devices
        if isinstance(entry, list) and len(entry) == 2 and all(isinstance(x, str) for x in entry)
    ]


def save_cache(devices: list[Device]) -> None:
    """Persist a *populated* discovery result (best-effort, atomic). An empty scan is
    often mDNS flakiness, so it never wipes a good cache — TTL + verify() handle the
    genuinely stale entries."""
    if not devices:
        return
    with contextlib.suppress(OSError):
        util.atomic_write(
            _cache_path(),
            lambda f: json.dump({"ts": time.time(), "devices": [list(d) for d in devices]}, f),
            prefix=".devices-",
        )


def verify(ip: str, *, timeout: float = VERIFY_TIMEOUT) -> bool:
    """True when `ip` answers on the cast control port — strong evidence the device is
    live even when an mDNS scan came back empty (flaky on multi-interface hosts)."""
    try:
        with socket.create_connection((ip, _CAST_PORT), timeout=timeout):
            return True
    except OSError:
        return False


# --- background scan singleton ----------------------------------------------
# One scan per process is plenty: devices don't come and go mid-session, and callers
# that need fresher data (the settings device picker) call scan_sync() themselves.

_lock = threading.Lock()
_done = threading.Event()
_result: list[Device] = []
_started = False


def start_background() -> None:
    """Kick off one LAN scan in a daemon thread (idempotent; no-op without catt on
    PATH). Started at TUI startup so the result is ready by the time the user casts;
    the worker refreshes the disk cache on success. Daemon thread + atomic cache write
    means quitting mid-scan is safe."""
    global _started
    with _lock:
        if _started:
            return
        if shutil.which("catt") is None:
            return  # stay "idle": resolve_device surfaces the missing binary itself
        _started = True
    threading.Thread(target=_scan_worker, name="cast-scan", daemon=True).start()


def _scan_worker() -> None:
    global _result
    devices = scan_sync()
    save_cache(devices)
    with _lock:
        _result = devices
    _done.set()
    _log.debug("scan in background completato: %d dispositivi", len(devices))


def get_devices(wait: float | None = 0.0) -> tuple[list[Device], str]:
    """Background-scan result as (devices, state): "fresh" when the scan finished (the
    list may legitimately be empty), "pending" while still running after waiting up to
    `wait` seconds (None → block until done), "idle" when no scan ever started."""
    with _lock:
        started = _started
    if not started:
        return ([], "idle")
    if not _done.wait(timeout=wait):
        return ([], "pending")
    with _lock:
        return (list(_result), "fresh")


def _reset() -> None:
    """Test helper: forget any prior scan so each test starts from a clean slate."""
    global _started, _result
    with _lock:
        _started = False
        _result = []
        _done.clear()
