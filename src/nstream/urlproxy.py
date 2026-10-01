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
import secrets
import shutil
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
_UPSTREAM_TIMEOUT = 30.0

_lock = threading.Lock()
_routes: dict[str, str] = {}
_server: ThreadingHTTPServer | None = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib signature
        pass  # never log paths: the route is a capability, the upstream a secret

    def do_HEAD(self) -> None:
        self._forward("HEAD")

    def do_GET(self) -> None:
        self._forward("GET")

    def _forward(self, method: str) -> None:
        with _lock:
            upstream = _routes.get(self.path.lstrip("/"))
        if upstream is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        headers = {"User-Agent": UA}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        req = urllib.request.Request(upstream, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=_UPSTREAM_TIMEOUT)  # noqa: S310
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
            # The reader seeking or stopping drops the connection: normal.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                shutil.copyfileobj(resp, self.wfile, _CHUNK)


def _ensure_server() -> ThreadingHTTPServer:
    global _server
    with _lock:
        if _server is None:
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            server.daemon_threads = True
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
