"""Loopback proxy that keeps debrid tokens out of child-process argv.

A debrid stream url carries the account token in its path, and `/proc/<pid>/cmdline` is
readable by every local user: handing it to `ffmpeg`/`ffprobe` as an argument published the
token for the whole run (seen in `pgrep -a ffmpeg`, 2026-10-01). `local_url(url)` registers
the url under a random path on an in-process server bound to 127.0.0.1 and returns that
loopback url instead; requests (Range included) are forwarded upstream and streamed back.

Only for children this process waits on (ffprobe, the remux ffmpeg, subtitle alignment):
the server lives as long as nstream does. Loopback urls (TorrServer, our own servers) are
returned unchanged — they carry no token and gain nothing from a second hop.
"""

from __future__ import annotations

import contextlib
import http.client
import re
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import log
from .net import UA

_log = log.get_logger("urlproxy")

# Upstream headers forwarded to the child: what a media reader needs for ranged reads.
_PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
_CHUNK = 256 * 1024
# Per socket operation. Short enough that a stalled upstream fails while the bounded
# ffprobe (6 s read) is still listening, long enough for a debrid's first byte.
_UPSTREAM_TIMEOUT = 10.0
# A long read (a whole film for the live producer) survives this many upstream drops: the
# proxy re-requests the rest with a Range from the last byte it delivered.
_RESUME_TRIES = 3
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
_RANGE = re.compile(r"bytes=(\d+)-(\d*)$")

_lock = threading.Lock()
_routes: dict[str, str] = {}
_server: _Server | None = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib signature
        pass  # never log paths: the route is a capability, the upstream a secret

    def do_HEAD(self) -> None:
        self._serve("HEAD")

    def do_GET(self) -> None:
        self._serve("GET")

    def _serve(self, method: str) -> None:
        # The reader seeking or stopping drops the connection: normal, on every write path
        # (an error reply to a client that already left used to print a traceback).
        with contextlib.suppress(*_CLIENT_GONE):
            self._forward(method)

    def _open(self, upstream: str, method: str, rng: str | None):
        headers = {"User-Agent": UA}
        if rng:
            headers["Range"] = rng
        req = urllib.request.Request(upstream, headers=headers, method=method)
        return urllib.request.urlopen(req, timeout=_UPSTREAM_TIMEOUT)  # noqa: S310

    def _forward(self, method: str) -> None:
        with _lock:
            upstream = _routes.get(self.path.lstrip("/"))
        if upstream is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        rng = self.headers.get("Range")
        try:
            resp = self._open(upstream, method, rng)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        except (urllib.error.URLError, OSError) as e:
            _log.warning("proxy: upstream irraggiungibile (%s)", type(e).__name__)
            self.send_error(HTTPStatus.BAD_GATEWAY)
            return
        with resp:
            self.send_response(resp.status)
            for name in _PASS_HEADERS:
                value = resp.headers.get(name)
                if value:
                    self.send_header(name, value)
            if not resp.headers.get("Content-Length"):
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if method == "HEAD":
                return
            resumable = resp.headers.get("Accept-Ranges") == "bytes" or resp.status == 206
            length = resp.headers.get("Content-Length")
            expected = int(length) if length and length.isdigit() else None
            self._copy(resp, upstream, rng if resp.status == 206 else None, resumable, expected)

    def _copy(
        self, resp, upstream: str, rng: str | None, resumable: bool, expected: int | None
    ) -> None:
        """Stream the body; on an upstream drop, re-request the rest from the last byte
        delivered (client errors propagate to `_serve`, which ends the request quietly).
        A drop is an error OR an early EOF: http.client returns b"" short of the
        Content-Length instead of raising."""
        m = _RANGE.match(rng or "bytes=0-")
        first, last = (int(m.group(1)), m.group(2)) if m else (0, "")
        sent = 0
        tries = 0
        while True:
            try:
                if resp is None:  # reopen the rest after a drop
                    resp = self._open(upstream, "GET", f"bytes={first + sent}-{last}")
                    if resp.status != 206:  # the upstream ignored the Range: cannot splice
                        resp.close()
                        self.close_connection = True
                        return
                chunk = resp.read(_CHUNK)
                if not chunk and expected is not None and sent < expected:
                    raise http.client.IncompleteRead(b"", expected - sent)
            except (OSError, http.client.HTTPException) as e:
                if resp is not None:
                    resp.close()
                    resp = None
                if not resumable or tries >= _RESUME_TRIES:
                    _log.warning("proxy: upstream interrotto (%s)", type(e).__name__)
                    self.close_connection = True
                    return
                tries += 1
                continue
            if not chunk:
                resp.close()
                return
            self.wfile.write(chunk)
            sent += len(chunk)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # Never print a traceback (the stdlib default) on the user's terminal: a client
        # that hung up is normal, anything else is logged once, without the path.
        exc = sys.exception()
        if not isinstance(exc, _CLIENT_GONE):
            _log.debug("proxy: errore di richiesta (%s)", type(exc).__name__)


def _ensure_server() -> _Server:
    global _server
    with _lock:
        if _server is None:
            server = _Server(("127.0.0.1", 0), _Handler)
            threading.Thread(target=server.serve_forever, name="urlproxy", daemon=True).start()
            _server = server
        return _server


def _is_local(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


def local_url(url: str) -> str:
    """A loopback url serving `url` without exposing it, or `url` itself when it is already
    local (or not http). The mapping lives for the rest of the process."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or _is_local(parts.hostname or ""):
        return url
    server = _ensure_server()
    route = secrets.token_urlsafe(16)
    with _lock:
        _routes[route] = url
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/{route}"
