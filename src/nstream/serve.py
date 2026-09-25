"""Tier-2 cast delivery: a minimal **Range-capable HTTP server** that serves one complete
local file to the Chromecast Default Media Receiver — plus, optionally, a side-loaded WebVTT
caption track at a second capability path (`served_sub_url`), with CORS headers the receiver
requires to fetch it. Replaces the detached `catt` file server
of ADR 0005 on the castbridge path — catt is itself a Python Range server, so owning this is
the correct implementation of exactly what the DMR needs (a complete, `Content-Length`'d,
`Range`/206 delivery), not a workaround. castbridge then LOADs this server's URL with metadata.

The DMR issues a GET with a `Range` header and expects a `206 Partial Content` with
`Content-Range`; a single seek may issue further range requests, so the server is threaded.

Two run modes:
- in-process (`serve_file`) for the interactive `follow` path (server thread dies with nstream);
- detached (`python -m nstream.serve <file> --bind <ip>`) for headless fire-and-return, where
  the server must outlive the CLI — `remux.py` records its PID for `--stop`/GC teardown, the
  same lifecycle the detached catt had.

Leaf module: stdlib (`http.server`/`socket`/`socketserver`) + `log`, imports nothing from
`cli`. The served path is local (no debrid token); request lines are logged only at debug.

Hardening: an administrator permits receiver access to ports 45000-47000; the URL carries a
**per-cast random token** (`/cast/<token>/stream.mp4`) — the only client that needs it (the
receiver) gets the full URL via LOAD, anyone scanning the port gets 404. The DMR sends no
auth headers, so a capability URL is the strongest gate that keeps the cast contract intact.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import log

_log = log.get_logger("serve")

# The receiver connects *back* to us over the LAN to fetch the file, so the bind port must be
# one the host firewall lets in. A default-deny UFW/nftables drops a random ephemeral port; the
# cast-serving range 45000-47000 is the one provisioned for this (the same band catt/skill-cast
# use). Bind inside it so Tier-2 works behind the firewall without a new rule; fall back to an
# ephemeral port only if the whole range is busy (then a firewall rule may be needed).
_CAST_PORT_LO, _CAST_PORT_HI = 45000, 47000
# ufw rule spec for the cast range — byte-identical to catt's range and skill-cast's
# `cast-screen fw-setup`, so the three share ONE rule (ufw dedups identical rules).
_CAST_RANGE_SPEC = f"{_CAST_PORT_LO}:{_CAST_PORT_HI}"


def lan_ip(target_ip: str) -> str:
    """The local IP on the interface that routes to `target_ip` (the TV). Uses the standard
    UDP-connect trick — no packet is sent, the kernel just selects the source address — so it is
    correct under multi-homing / VPN, unlike a hostname lookup. Falls back to 127.0.0.1 (which
    the TV can't reach, surfacing a clean failure) if resolution fails."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 9))  # discard port; UDP connect only sets the route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def new_token() -> str:
    """A fresh per-cast URL token (unguessable path segment)."""
    return secrets.token_urlsafe(16)


def url_path(token: str) -> str:
    """The secret URL path the receiver must hit for the media; everything else is 404."""
    return f"/cast/{token}/stream.mp4"


def sub_url_path(token: str) -> str:
    """The secret URL path for the optional side-loaded WebVTT caption track."""
    return f"/cast/{token}/subs.vtt"


def served_url(ip: str, port: int, token: str) -> str:
    return f"http://{ip}:{port}{url_path(token)}"


def served_sub_url(ip: str, port: int, token: str) -> str:
    return f"http://{ip}:{port}{sub_url_path(token)}"


def _lan_subnet(host_ip: str) -> str:
    """The /24 the host's LAN IP belongs to (e.g. 192.168.1.75 → 192.168.1.0/24), matching
    skill-cast's `cast-screen lan_subnet()` so the ufw rule is scoped identically."""
    return ".".join(host_ip.split(".")[:3]) + ".0/24"


def ensure_firewall(bind_ip: str) -> None:
    """Compatibility hook: playback never changes host firewall policy (ADR 0034).

    Connection failures surface `firewall_hint`; administrators apply their own
    least-privilege rule explicitly. Do not invoke sudo, even when passwordless.
    """


def firewall_hint(bind_ip: str, port: int | None = None) -> str:
    """An actionable line to print when a Tier-2 cast fails to start: the receiver likely can't
    reach our file server through the host firewall. Names the exact ufw rule to add."""
    subnet = _lan_subnet(bind_ip)
    where = f"su {bind_ip}:{port}" if port else f"su {bind_ip}"
    return (
        f"nstream: la TV non raggiunge il file server {where} — probabile firewall. "
        f"Apri la porta dalla LAN: sudo ufw allow from {subnet} to any port "
        f"{_CAST_RANGE_SPEC} proto tcp"
    )


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single-range `bytes=start-end` header against a file of `size` bytes, returning an
    inclusive `(start, end)` byte range, or None when the header is absent/unsatisfiable/multi-range
    (the caller then serves the full 200 or a 416). Supports `start-`, `start-end`, `-suffix`."""
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :]
    if "," in spec:  # multi-range: not needed by the DMR, serve full
        return None
    first, _, last = spec.partition("-")
    try:
        if first == "":  # suffix range: last N bytes
            n = int(last)
            if n <= 0:
                return None
            start = max(0, size - n)
            return (start, size - 1)
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return (start, min(end, size - 1))


class RangeFileHandler(BaseHTTPRequestHandler):
    """Serves `self.server.file_path` (media) and/or `self.server.sub_path` (WebVTT track) with
    Range support, each on its own secret token path. GET/HEAD/OPTIONS only; anything else is 404
    (no listing, no probing). All responses carry CORS (the receiver fetches the track cross-site).
    """

    protocol_version = "HTTP/1.1"
    # Don't advertise the Python/stdlib versions to whoever scans the open cast port.
    server_version = "nstream"
    sys_version = ""
    _CHUNK = 256 * 1024
    # Narrow the inherited `server` type so `self.server.file_path` resolves (lazy annotation
    # via `from __future__ import annotations`; _FileServer is defined below).
    server: _FileServer

    def _mask(self, text: str) -> str:
        """The token is a capability: keep it out of the (local, but persistent) log."""
        return text.replace(self.server.token, "<token>")

    def log_message(self, format: str, *args) -> None:
        _log.debug("serve %s", self._mask(format % args))

    def _resolve_target(self) -> tuple[str, str] | None:
        """Map the request path to (local_file, content_type), or None (→404). Two capability
        paths: the media file and the optional side-loaded WebVTT caption track. Constant-time
        compares so the token can't be probed byte-by-byte."""
        srv = self.server
        req = self.path.encode()
        if srv.file_path and secrets.compare_digest(req, srv.url_path.encode()):
            ct = (
                "video/mp4"
                if srv.file_path.lower().endswith(".mp4")
                else "application/octet-stream"
            )
            return srv.file_path, ct
        if srv.sub_path and secrets.compare_digest(req, srv.sub_url_path.encode()):
            return srv.sub_path, "text/vtt; charset=utf-8"
        return None

    def _send_cors(self) -> None:
        # The Default Media Receiver fetches a side-loaded caption track cross-origin and
        # requires CORS; harmless on the media response too. `*` is safe: the URL already
        # carries an unguessable per-cast capability token (see module docstring).
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_HEAD(self) -> None:
        self._respond(write_body=False)

    def do_GET(self) -> None:
        self._respond(write_body=True)

    def do_OPTIONS(self) -> None:
        # CORS preflight the Cast receiver may send before fetching the VTT track.
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _method_not_allowed(self) -> None:
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed

    def _respond(self, *, write_body: bool) -> None:
        # The first request proves the receiver can reach us — the key discriminator when a
        # Tier-2 cast fails to start (no request → network/firewall; request but no playback
        # → media/receiver). One INFO line per server; later requests stay at debug.
        if not self.server.got_request:
            self.server.got_request = True
            _log.info(
                "serve: prima richiesta da %s: %s %s (Range=%s)",
                self.client_address[0], self.command, self._mask(self.path),
                self.headers.get("Range") or "-",
            )  # fmt: skip
        # Capability check: only the exact per-cast token paths (media / VTT) are served; the
        # firewall lets the whole LAN in, so any other path (scanners, other hosts) gets 404.
        target = self._resolve_target()
        if target is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path, content_type = target
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        rng = _parse_range(self.headers.get("Range", ""), size)
        if self.headers.get("Range") and rng is None and self._unsatisfiable(size):
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self._send_cors()
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if rng is None:
            start, end = 0, size - 1
            status = HTTPStatus.OK
        else:
            start, end = rng
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not write_body:
            return
        self._send_body(path, start, length)

    def _unsatisfiable(self, size: int) -> bool:
        """A Range header was present but unparsed: treat as 416 only when it's a well-formed but
        out-of-bounds byte range (`bytes=<start>-`), not a malformed/multi-range header (→ 200)."""
        spec = self.headers.get("Range", "")[len("bytes=") :]
        if "," in spec or "-" not in spec:
            return False
        first = spec.partition("-")[0]
        return first.isdigit() and int(first) >= size

    def _send_body(self, path: str, start: int, length: int) -> None:
        remaining = length
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(self._CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # The receiver closed the connection (seek / stop). Normal; not an error.
            _log.debug("serve: client chiuso durante lo stream")
        except OSError as e:
            _log.warning("serve: errore I/O: %s", e)


class _FileServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr,
        file_path: str | None,
        token: str | None = None,
        sub_path: str | None = None,
    ):
        super().__init__(addr, RangeFileHandler)
        self.file_path = file_path  # media file (None → sub-only server, e.g. Tier-1 direct)
        self.sub_path = sub_path  # optional side-loaded WebVTT caption track
        self.token = token or new_token()  # per-cast capability (see module docstring)
        self.url_path = url_path(self.token)
        self.sub_url_path = sub_url_path(self.token)
        self.got_request = False  # first-request INFO latch (see RangeFileHandler._respond)


def _make_server(
    bind_ip: str,
    file_path: str | None,
    preferred_port: int = 0,
    token: str | None = None,
    sub_path: str | None = None,
) -> _FileServer:
    """Bind a `_FileServer`, preferring the firewall-allowed cast port range so the receiver can
    actually reach us. `preferred_port > 0` forces that exact port; otherwise scan the range and
    fall back to an ephemeral port if it's fully busy."""
    if preferred_port:
        return _FileServer((bind_ip, preferred_port), file_path, token, sub_path)
    for port in range(_CAST_PORT_LO, _CAST_PORT_HI + 1):
        try:
            return _FileServer((bind_ip, port), file_path, token, sub_path)
        except OSError:
            continue
    _log.warning(
        "nessuna porta libera in %d-%d; uso una effimera (può servire una regola firewall)",
        _CAST_PORT_LO,
        _CAST_PORT_HI,
    )
    return _FileServer((bind_ip, 0), file_path, token, sub_path)


def serve_file(
    file_path: str | None, bind_ip: str, sub_path: str | None = None
) -> tuple[_FileServer, int, threading.Thread]:
    """Start a threaded Range server for `file_path` (and/or a side-loaded WebVTT `sub_path`)
    bound to `bind_ip:0` (ephemeral port). Returns `(server, port, thread)`; the caller builds
    URLs via `served_url`/`served_sub_url(bind_ip, port, server.token)` and shuts down with
    `server.shutdown()`. The thread is a daemon (dies with the process), so this is the
    in-process (follow) mode — headless uses the `__main__` detached entrypoint."""
    server = _make_server(bind_ip, file_path, sub_path=sub_path)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, name="nstream-serve", daemon=True)
    thread.start()
    return server, port, thread


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = Path(base) / "nstream"
    with contextlib.suppress(OSError):
        d.mkdir(parents=True, exist_ok=True)
    return d


def spawn_detached(
    bind_ip: str, *, file_path: str | None = None, sub_path: str | None = None
) -> tuple[int, int, str] | None:
    """Spawn a detached `python -m nstream.serve` serving `file_path` and/or a WebVTT `sub_path`
    on `bind_ip`, returning (pid, port, token) once it announces them, or None on failure. Detached
    (new session) so it outlives a headless return — the receiver fetches from it for the whole
    runtime. The per-cast URL token is generated by the server and read from its stdout (never on
    the command line, where `ps` would show it). Shared by `remux` (media) and `caster` (sub-only).
    """
    cmd = [sys.executable, "-m", "nstream.serve", "--bind", bind_ip]
    if file_path:
        cmd.append(file_path)
    if sub_path:
        cmd += ["--subs", sub_path]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        _log.warning("serve detach fallito: %s", e)
        return None
    if proc.stdout is None:
        kill_detached(proc.pid)
        return None
    line = proc.stdout.readline().strip()
    if not line.startswith("PORT="):
        _log.warning("serve: porta non annunciata (%r)", line[:40])
        kill_detached(proc.pid)
        return None
    try:
        port = int(line[len("PORT=") :])
    except ValueError:
        kill_detached(proc.pid)
        return None
    token_line = proc.stdout.readline().strip()
    if not token_line.startswith("TOKEN=") or token_line == "TOKEN=":
        _log.warning("serve: token non annunciato")
        kill_detached(proc.pid)
        return None
    return proc.pid, port, token_line[len("TOKEN=") :]


def kill_detached(pid: int | None) -> None:
    """SIGTERM a detached serve process by its process group (it is a session leader)."""
    if not pid:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(os.getpgid(pid), signal.SIGTERM)


# --- standalone subtitle server registry (Tier-1 direct cast) ----------------
# A Tier-1 (direct) cast plays a remote debrid URL, so its side-loaded WebVTT track needs a
# small local server of its own. Only one cast plays at a time on the TV, so a single-slot
# state suffices: any new cast — or `--stop` — reaps the previous one. (Tier-2 serves the VTT
# from the same server as the media; that lifecycle stays in `remux`.)


def _sub_server_state() -> Path:
    return _cache_dir() / "sub-server.pid"


def persist_sub(vtt_path: str) -> str | None:
    """Copy a per-play VTT into the cache so the DETACHED server can keep serving it after
    the play's work_dir is cleaned up (fire-and-return exits while the receiver may still
    re-fetch the track, e.g. on seek — serving a deleted path broke that silently). The
    copy's lifecycle is the server's: reaped together in `reap_sub_server`. None on failure
    (caller degrades to the old serve-from-work_dir behaviour)."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "nstream" / "subs"
    try:
        base.mkdir(parents=True, exist_ok=True)
        fd, dst = tempfile.mkstemp(prefix="cast-sub-", suffix=".vtt", dir=str(base))
        with os.fdopen(fd, "wb") as out, open(vtt_path, "rb") as src:
            shutil.copyfileobj(src, out)
        return dst
    except OSError:
        return None


def register_sub_server(pid: int, persist_path: str | None = None) -> None:
    """Record the detached standalone subtitle server (and its persisted VTT copy, if any)
    so a later cast / `--stop` can reap both."""
    with contextlib.suppress(OSError):
        _sub_server_state().write_text(f"{pid}\n{persist_path or ''}")


def reap_sub_server() -> bool:
    """Kill and forget a leftover standalone subtitle server, removing its persisted VTT
    copy. True if there was one (best-effort, idempotent)."""
    p = _sub_server_state()
    try:
        lines = p.read_text().splitlines()
        pid = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return False
    kill_detached(pid)
    if len(lines) > 1 and lines[1].strip():
        with contextlib.suppress(OSError):
            os.unlink(lines[1].strip())
    with contextlib.suppress(OSError):
        p.unlink()
    return True


def _main(argv: list[str] | None = None) -> int:
    """Detached entrypoint: `python -m nstream.serve <file> --bind <ip> [--port N]`. Binds, prints
    a `PORT=<n>` line then a `TOKEN=<t>` line on stdout (so the parent learns the ephemeral port
    and the per-cast URL token — generated here, never on the command line where `ps` would show
    it), then serves forever until killed (SIGTERM from `remux.stop`/GC). All other output goes
    to the log."""
    ap = argparse.ArgumentParser(prog="nstream.serve")
    ap.add_argument("file", nargs="?")  # media file; omit for a sub-only server (Tier-1 direct)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--subs")  # optional side-loaded WebVTT caption track
    args = ap.parse_args(argv)
    # Detached process: nothing has configured logging (cli._entry does it for the TUI), so
    # without this the first-request INFO — the network-vs-media discriminator on a Tier-2
    # startup failure — would be lost exactly in the headless case.
    log.setup_logging()
    if args.file and not os.path.isfile(args.file):
        print(f"serve: file non trovato: {args.file}", file=sys.stderr)
        return 2
    if args.subs and not os.path.isfile(args.subs):
        print(f"serve: sottotitoli non trovati: {args.subs}", file=sys.stderr)
        return 2
    if not args.file and not args.subs:
        print("serve: né file né --subs specificati", file=sys.stderr)
        return 2
    server = _make_server(args.bind, args.file, preferred_port=args.port, sub_path=args.subs)
    port = server.server_address[1]
    # The parent reads exactly these two lines to learn port+token, then leaves us running.
    sys.stdout.write(f"PORT={port}\nTOKEN={server.token}\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
