"""Local peer-to-peer playback engine: drives a TorrServer instance so pure-torrent
streams (Torrentio with debrid off) play without any paid service.

TorrServer is an external single-binary HTTP torrent server (like mpv/catt/fzf, it is
user-installed, never bundled). nstream finds an already-running instance or spawns one,
adds the torrent by infoHash, waits for the initial read-ahead buffer, then hands a plain
`http://…/stream?link=…` url to `player.play()` / `caster.cast()` — the *same contract* as
a debrid url, so the rest of the pipeline is unchanged. The stream host is `127.0.0.1` for
mpv and the machine's LAN IP for casting (so the Chromecast can reach it).

Imports only `config`/`log`/`util` (+ stdlib): a leaf below `cli`, like `caster`/`player`.
Everything is best-effort — a missing binary or unreachable server raises `EngineUnavailable`
with a user-facing message; it never crashes the picker. REST field names follow TorrServer's
`/swagger` schema; access them defensively so a schema drift degrades rather than throws.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import log, ui
from .config import Config, Stream

_log = log.get_logger("engine")

BINARY = "TorrServer"  # canonical name (used in user-facing messages)
# The executable's name varies by build: upstream releases ship "TorrServer", the Arch
# `torrserver-bin` package installs lower-case "torrserver". Accept either on PATH.
_BINARY_NAMES = ("TorrServer", "torrserver")
_ECHO_TIMEOUT = 2.0  # health probe
_SPAWN_READY_TIMEOUT = 20.0  # wait for a freshly spawned server to answer /echo
_BUFFER_TIMEOUT = 120.0  # wait for the initial read-ahead before launching the player
_HTTP_TIMEOUT = 10.0

# Process-wide singleton: the base url of the running server and the handle to the
# instance *we* spawned (so atexit only kills ours, never a user's long-lived one).
_lock = threading.Lock()
_base_url: str | None = None
_spawned: subprocess.Popen | None = None


class EngineUnavailable(Exception):
    """The local engine can't serve this stream (binary missing, server down, add failed)."""


# --- low-level HTTP to the local server ----------------------------------


def _get(base: str, path: str, *, timeout: float = _HTTP_TIMEOUT) -> dict:
    req = urllib.request.Request(f"{base}{path}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _post(base: str, path: str, body: dict, *, timeout: float = _HTTP_TIMEOUT) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base}{path}", data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _alive(base: str) -> bool:
    """True if a TorrServer answers /echo on `base`."""
    try:
        req = urllib.request.Request(f"{base}/echo")
        with urllib.request.urlopen(req, timeout=_ECHO_TIMEOUT):
            return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


# --- addressing ----------------------------------------------------------


def _lan_ip() -> str:
    """The machine's primary LAN IP (so a Chromecast can reach the local server). Uses the
    standard UDP-connect trick — no packet is sent. Falls back to 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _download_dir(cfg: Config) -> str:
    if cfg.engine_download_dir:
        return cfg.engine_download_dir
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return str(Path(base) / "nstream" / "torrents")


def _binary() -> str | None:
    """Path to the TorrServer executable under any of its known names, or None."""
    return next((p for name in _BINARY_NAMES if (p := shutil.which(name))), None)


def installed() -> bool:
    """Whether the TorrServer binary is on PATH (for the settings health check)."""
    return _binary() is not None


# Interface name prefixes used by common VPNs (WireGuard, OpenVPN, Proton, Nord, Mullvad).
_VPN_IFACE_PREFIXES = ("tun", "tap", "wg", "proton", "nordlynx", "mullvad", "wgpia", "pia")


def vpn_active() -> bool:
    """Best-effort: True if a VPN-style network interface (tun*/wg*/…) is up. Read-only,
    stdlib only (reads /sys/class/net). Used to warn before P2P streaming exposes the IP to
    peers. A heuristic, not a guarantee — split-tunnel or unusual setups may evade it."""
    net = "/sys/class/net"
    try:
        names = os.listdir(net)
    except OSError:
        return False
    for n in names:
        if not n.startswith(_VPN_IFACE_PREFIXES):
            continue
        try:
            with open(os.path.join(net, n, "operstate")) as f:
                if f.read().strip() in ("up", "unknown"):  # wg often reports "unknown" while up
                    return True
        except OSError:
            return True  # a VPN-named iface exists but has no operstate → assume up
    return False


# --- lifecycle -----------------------------------------------------------


def ensure_running(cfg: Config) -> str:
    """Return the base url of a running TorrServer, reusing an existing instance (the user's
    or one we already spawned) or launching one on `cfg.engine_port`. Raises EngineUnavailable
    if it can't be reached and the binary isn't installed."""
    global _base_url, _spawned
    with _lock:
        port = cfg.engine_port
        base = f"http://127.0.0.1:{port}"
        if _base_url and _alive(_base_url):
            return _base_url
        if _alive(base):  # someone already runs one on this port
            _base_url = base
            return base
        binpath = _binary()
        if binpath is None:
            raise EngineUnavailable(
                f"{BINARY} non trovato — installalo (es. `yay -S torrserver-bin`) "
                "o passa a un provider debrid nei settings"
            )
        path = _download_dir(cfg)
        with contextlib.suppress(OSError):
            Path(path).mkdir(parents=True, exist_ok=True)
        try:
            _spawned = subprocess.Popen(  # noqa: S603 — long-lived, like mpv
                [binpath, "--port", str(port), "--path", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Own session: the server must be able to outlive nstream (detach_spawned
                # hands it off to a fire-and-return cast) — no SIGHUP/SIGINT from our tty.
                start_new_session=True,
            )
        except OSError as e:
            raise EngineUnavailable(f"avvio {BINARY} fallito: {e}") from e
        atexit.register(_shutdown)
        deadline = time.monotonic() + _SPAWN_READY_TIMEOUT
        while time.monotonic() < deadline:
            if _alive(base):
                _base_url = base
                _log.info("%s avviato su :%d (dati in %s)", BINARY, port, path)
                _configure_cache(base, cfg)
                return base
            if _spawned.poll() is not None:
                raise EngineUnavailable(f"{BINARY} è uscito durante l'avvio")
            time.sleep(0.3)
        raise EngineUnavailable(f"{BINARY} non ha risposto entro {_SPAWN_READY_TIMEOUT:.0f}s")


def _configure_cache(base: str, cfg: Config) -> None:
    """Best-effort: set the in-memory read-ahead cache size. Schema-tolerant — a failure
    just leaves TorrServer's own default in place."""
    with contextlib.suppress(Exception):
        settings = _post(base, "/settings", {"action": "get"})
        settings["CacheSize"] = cfg.engine_cache_mb * 1024 * 1024
        _post(base, "/settings", {"action": "set", **settings})


def _shutdown() -> None:
    """Terminate only the server we spawned (atexit)."""
    global _spawned
    if _spawned and _spawned.poll() is None:
        with contextlib.suppress(OSError):
            _spawned.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            _spawned.wait(timeout=5)
    _spawned = None


def detach_spawned() -> None:
    """Let a TorrServer we spawned outlive nstream's exit. A headless fire-and-return cast
    (`--json --cast` without `--follow`) hands the engine url to the Chromecast and returns:
    the atexit `_shutdown` would kill the server mid-playback, so drop our handle instead
    (the process runs in its own session and survives). No-op when nothing was spawned."""
    global _spawned
    with _lock:
        proc, _spawned = _spawned, None
    if proc is not None:
        _log.info("%s lasciato vivo (pid %d) per il cast in corso", BINARY, proc.pid)


# --- torrent add / stream-url construction -------------------------------


def magnet_from_stream(stream: Stream) -> str:
    """Build a magnet from the Stremio stream's infoHash + tracker `sources` (better peer
    discovery than the bare hash). Public so the native debrid resolver (`debrid.py`) can
    reuse the same magnet — TorBox/Premiumize add it just like TorrServer does."""
    ih = stream["infoHash"]
    parts = [f"magnet:?xt=urn:btih:{ih}"]
    title = (stream.get("title") or "").split("\n", 1)[0].strip()
    if title:
        parts.append("dn=" + urllib.parse.quote(title))
    for src in stream.get("sources") or []:
        if src.startswith("tracker:"):
            parts.append("tr=" + urllib.parse.quote(src[len("tracker:") :]))
    return parts[0] + ("&" + "&".join(parts[1:]) if len(parts) > 1 else "")


def _largest_index(files: list[dict]) -> int:
    """1-based index of the biggest file, for streams that don't pin a fileIdx."""
    if not files:
        return 1
    best = max(files, key=lambda f: f.get("length", 0))
    return int(best.get("id", 1))


def resolve(cfg: Config, stream: Stream) -> str:
    """Add the torrent to the running server and return the HTTP stream url for the chosen
    file. The host is the machine's LAN IP — reachable both by local mpv and by a Chromecast,
    so one url serves both playback paths (loopback would be invisible to the TV)."""
    base = ensure_running(cfg)
    link = magnet_from_stream(stream)
    try:
        added = _post(base, "/torrents", {"action": "add", "link": link, "save_to_db": False})
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        raise EngineUnavailable(f"aggiunta torrent fallita: {e}") from e
    file_hash = added.get("hash") or stream["infoHash"]
    file_idx = stream.get("fileIdx")
    index = (
        file_idx + 1 if isinstance(file_idx, int) else _largest_index(added.get("file_stats", []))
    )
    _wait_buffer(base, file_hash)
    return f"http://{_lan_ip()}:{cfg.engine_port}/stream?link={file_hash}&index={index}&play"


def _torrent_stat(base: str, file_hash: str) -> dict:
    with contextlib.suppress(Exception):
        return _post(base, "/torrents", {"action": "get", "hash": file_hash})
    return {}


def _wait_buffer(base: str, file_hash: str) -> None:
    """Poll the torrent until its initial read-ahead buffer is filled, printing progress to
    stderr (peers + preloaded MB, with a % of the read-ahead target when the server reports
    one). Returns as soon as it's playable; Ctrl-C aborts. On timeout: with zero bytes ever
    buffered (dead torrent) raises EngineUnavailable so the caller degrades to the next
    candidate; with a partial buffer it returns anyway, after an explicit notice.
    Avoids the start-of-playback stutter you'd get launching mpv against an empty buffer."""
    deadline = time.monotonic() + _BUFFER_TIMEOUT
    preloaded = 0
    filled = False
    try:
        while time.monotonic() < deadline:
            st = _torrent_stat(base, file_hash)
            preloaded = st.get("preloaded_bytes", 0)
            preload = st.get("preload_size", 0)
            peers = st.get("active_peers", st.get("connected_seeders", 0))
            mb = preloaded / (1024 * 1024)
            pct = f" ({100 * preloaded / preload:3.0f}%)" if preload else ""
            msg = f"\r{ui.g().globe} buffering P2P… peer {peers}  {mb:6.1f} MB{pct}"
            print(msg, end="", file=sys.stderr)
            # Ready once the read-ahead window is full, or some data is buffered with peers.
            if (preload and preloaded >= preload) or (preloaded > 2 * 1024 * 1024 and peers):
                filled = True
                break
            time.sleep(0.5)
        print("", file=sys.stderr)  # newline after the \r progress line
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        raise EngineUnavailable("buffering interrotto") from None
    if filled:
        return
    if preloaded <= 0:  # dead torrent: never a single byte → let the caller degrade
        raise EngineUnavailable(f"nessun peer / buffer vuoto dopo {_BUFFER_TIMEOUT:.0f}s di attesa")
    print(
        f"nstream: {ui.g().warn} buffer parziale dopo il timeout, provo comunque…",
        file=sys.stderr,
    )
