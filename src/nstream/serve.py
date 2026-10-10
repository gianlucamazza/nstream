"""Tier-2 cast delivery: a minimal **Range-capable HTTP server** that serves one complete
local file to the Chromecast Default Media Receiver — plus, optionally, a side-loaded WebVTT
caption track at a second capability path (`served_sub_url`), with CORS headers the receiver
requires to fetch it. ADR 0045 Phase 1 also Range-proxies a remote debrid url on the same
capability path (`upstream=` / detached `--proxy` via stdin) so a catt-only host can hand
the TV a LAN `Content-Length`'d 206 instead of a WAN url. Replaces the detached `catt` file
server of ADR 0005 on the castbridge path — catt is itself a Python Range server, so owning
this is the correct implementation of exactly what the DMR needs (a complete, `Content-Length`'d,
`Range`/206 delivery), not a workaround. castbridge then LOADs this server's URL with metadata.

The DMR issues a GET with a `Range` header and expects a `206 Partial Content` with
`Content-Range`; a single seek may issue further range requests, so the server is threaded.

A third capability route serves a live HLS-TS directory (ADR 0039, `live`): the playlist and
its whitelisted segment names only, each served segment advancing the producer's play head.

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
import json
import os
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import languages, live, log, srt, urlproxy, util

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


def url_path(token: str, name: str = "stream.mp4") -> str:
    """The secret URL path the receiver must hit for the media; everything else is 404."""
    return f"/cast/{token}/{name}"


def sub_url_path(token: str) -> str:
    """The secret URL path for the optional side-loaded WebVTT caption track."""
    return f"/cast/{token}/subs.vtt"


def hls_url_path(token: str) -> str:
    """The secret URL prefix of a live HLS directory (playlist + segments)."""
    return f"/cast/{token}/hls/"


def served_hls_url(ip: str, port: int, token: str, name: str = live.PLAYLIST) -> str:
    """The playlist url a live cast LOADs (`name`: the master when subtitles ride along)."""
    return f"http://{ip}:{port}{hls_url_path(token)}{name}"


def served_url(ip: str, port: int, token: str, name: str = "stream.mp4") -> str:
    return f"http://{ip}:{port}{url_path(token, name)}"


def served_sub_url(ip: str, port: int, token: str) -> str:
    return f"http://{ip}:{port}{sub_url_path(token)}"


def caption_kwargs(bind_ip: str, port: int, token: str, lang: str | None) -> dict[str, str]:
    """The `bridge.cast_load` args side-loading the served WebVTT as a caption track: its
    url, the BCP-47 language the receiver expects and a display name for its track menu.
    The one place both cast tiers (direct and Tier-2) build them."""
    out = {"subtitle_url": served_sub_url(bind_ip, port, token)}
    if lang:
        out["subtitle_lang"] = languages.bcp47(lang)
        out["subtitle_name"] = languages.name(lang)
    return out


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


_HLS_PLAYLIST_TYPE = "application/vnd.apple.mpegurl"


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
        want = srv.url_path.encode()
        media_hit = (srv.file_path or srv.upstream) and len(req) == len(want)
        if media_hit and secrets.compare_digest(req, want):
            if srv.upstream and not srv.file_path:
                return "", srv.upstream_type or "video/mp4"
            ct = (
                "video/mp4"
                if (srv.file_path or "").lower().endswith(".mp4")
                else "application/octet-stream"
            )
            return srv.file_path or "", ct
        if srv.sub_path and secrets.compare_digest(req, srv.sub_url_path.encode()):
            return srv.sub_path, "text/vtt; charset=utf-8"
        prefix = srv.hls_prefix.encode()
        if (
            srv.hls_dir
            and len(req) > len(prefix)
            and secrets.compare_digest(req[: len(prefix)], prefix)
        ):
            name = req[len(prefix) :].decode("ascii", errors="replace")
            # Whitelisted names only: no traversal, and ffmpeg's log beside the segments
            # (or a `.tmp` being written) is never served.
            if live.is_playlist_name(name):
                return os.path.join(srv.hls_dir, name), _HLS_PLAYLIST_TYPE
            if live.segment_id(name) is not None:
                return os.path.join(srv.hls_dir, name), "video/mp2t"
            if live.vtt_id(name) is not None:  # a subtitle rendition's cue segment
                return os.path.join(srv.hls_dir, name), "text/vtt; charset=utf-8"
        return None

    def _send_cors(self) -> None:
        # The Default Media Receiver fetches a side-loaded caption track cross-origin and
        # requires CORS; harmless on the media response too. `*` is safe: the URL already
        # carries an unguessable per-cast capability token (see module docstring).
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_HEAD(self) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self._respond(write_body=False)

    def do_GET(self) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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
        # Record before the reply: waiting until after the body races the client
        # (test_hls_route_serves_playlist_and_segments on 3.14, run 38008509413).
        producer = self.server.producer
        if producer is not None and write_body:
            producer.on_request(os.path.basename(path))
        self.server.touch(+1)
        try:
            self._serve_target(path, content_type, write_body=write_body)
        finally:
            self.server.touch(-1)

    def _serve_playlist(self, path: str, *, write_body: bool) -> None:
        """A live playlist, in film time (`live.film_time_playlist`): small and growing, so
        always whole (no Range) and never cached."""
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        name = os.path.basename(path)
        if live.is_master_name(name):
            text = live.master_with_codecs(text, self.server.generation_codecs(path))
        else:
            text = live.film_time_playlist(text, self.server.playlist_base(path))
        body = text.encode()
        self.send_response(HTTPStatus.OK)
        self._send_cors()
        self.send_header("Content-Type", _HLS_PLAYLIST_TYPE)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def _serve_shifted_vtt(self, path: str, shift: float, *, write_body: bool) -> None:
        """A WebVTT moved by the live cast's `--sub-shift` (whole body, no Range)."""
        text = srt.decode(path)
        if text is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = srt.shift_vtt(text, shift).encode()
        self.send_response(HTTPStatus.OK)
        self._send_cors()
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def _serve_target(self, path: str, content_type: str, *, write_body: bool) -> None:
        if self.server.upstream and not path:
            self._serve_upstream(content_type, write_body=write_body)
            return
        if content_type == _HLS_PLAYLIST_TYPE:
            self._serve_playlist(path, write_body=write_body)
            return
        shift = self.server.sub_shift() if content_type.startswith("text/vtt") else 0.0
        if shift:
            self._serve_shifted_vtt(path, shift, write_body=write_body)
            return
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
        if content_type == _HLS_PLAYLIST_TYPE:
            self.send_header("Cache-Control", "no-cache")  # a live playlist grows
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not write_body:
            return
        self._send_body(path, start, length)

    def _serve_upstream(self, content_type: str, *, write_body: bool) -> None:
        """Range-serve a remote url toward the DMR (ADR 0045 Phase 1).

        Pass through an upstream 206. Never synthesize 206 by discarding from a
        full GET (that scales with the seek offset at WAN speed). When the upstream
        does not honour Range we do not advertise Accept-Ranges. Never logs the url."""
        srv = self.server
        upstream = srv.upstream
        if not upstream:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = srv.upstream_length
        raw_range = self.headers.get("Range", "") if srv.upstream_ranged else ""
        rng = _parse_range(raw_range, size) if raw_range and size is not None else None
        if raw_range and size is not None and rng is None and self._unsatisfiable(size):
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self._send_cors()
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not write_body and size is not None:
            if rng is None:
                start, end, status = 0, size - 1, HTTPStatus.OK
            else:
                start, end, status = rng[0], rng[1], HTTPStatus.PARTIAL_CONTENT
            self._upstream_headers(
                status, content_type, end - start + 1, start, end, size,
                accept_ranges=srv.upstream_ranged,
            )  # fmt: skip
            return

        up_range = None
        if rng is not None:
            up_range = f"bytes={rng[0]}-{rng[1]}"
        elif raw_range and size is None and srv.upstream_ranged:
            up_range = raw_range
        method = "GET" if write_body else "HEAD"
        try:
            resp = urlproxy.open_upstream(upstream, method, up_range)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            e.close()
            return
        except (urllib.error.URLError, OSError) as e:
            _log.warning("serve: upstream irraggiungibile (%s)", type(e).__name__)
            self.send_error(HTTPStatus.BAD_GATEWAY)
            return
        with resp:
            if resp.status == 206:
                self._relay_partial(resp, content_type, write_body=write_body)
                return
            # 200: the upstream ignored Range (or none was sent). Relay as 200.
            # Never synthesize 206; never claim Accept-Ranges we cannot honour.
            length_h = resp.headers.get("Content-Length")
            expected = int(length_h) if length_h and str(length_h).isdigit() else None
            self.send_response(HTTPStatus.OK)
            self._send_cors()
            self.send_header("Content-Type", content_type)
            if srv.upstream_ranged:
                self.send_header("Accept-Ranges", "bytes")
            if expected is not None:
                self.send_header("Content-Length", str(expected))
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if write_body:
                urlproxy.copy_body(resp, self.wfile.write, upstream, None, False, expected)

    def _upstream_headers(
        self,
        status: int,
        content_type: str,
        length: int,
        start: int,
        end: int,
        size: int,
        *,
        accept_ranges: bool = True,
    ) -> None:
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", content_type)
        if accept_ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

    def _relay_partial(self, resp, content_type: str, *, write_body: bool) -> None:
        cr = resp.headers.get("Content-Range")
        cl = resp.headers.get("Content-Length")
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self._send_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        if cl:
            self.send_header("Content-Length", cl)
        if cr:
            self.send_header("Content-Range", cr)
        self.end_headers()
        upstream = self.server.upstream
        if write_body and upstream:
            expected = int(cl) if cl and str(cl).isdigit() else None
            # Resume from the upstream's Content-Range start, never a raw suffix.
            up_range = urlproxy.range_from_content_range(cr)
            raw = self.headers.get("Range")
            if up_range is None and urlproxy.is_start_end_range(raw):
                up_range = raw
            urlproxy.copy_body(
                resp, self.wfile.write, upstream,
                up_range, up_range is not None, expected,
            )  # fmt: skip

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
        hls_dir: str | None = None,
        upstream: str | None = None,
        upstream_type: str = "",
        upstream_length: int | None = None,
        upstream_ranged: bool = False,
        media_name: str = "stream.mp4",
    ):
        super().__init__(addr, RangeFileHandler)
        self.hls_dir = hls_dir  # live HLS directory (ADR 0039)
        self.producer: live.Producer | None = None  # its producer, when this process owns it
        self.file_path = file_path  # media file (None → sub-only / upstream proxy)
        self.sub_path = sub_path  # optional side-loaded WebVTT caption track
        self.upstream = upstream  # remote url to Range-proxy (ADR 0045); never logged
        self.upstream_type = upstream_type
        self.upstream_length = upstream_length
        self.upstream_ranged = upstream_ranged
        self.token = token or new_token()  # per-cast capability (see module docstring)
        self.url_path = url_path(self.token, media_name)
        self.sub_url_path = sub_url_path(self.token)
        self.hls_prefix = hls_url_path(self.token)
        self.got_request = False  # first-request INFO latch (see RangeFileHandler._respond)
        # Idle tracking for the detached server's exit (`_main`): only requests that hit
        # the token paths count, so LAN scanners can't keep a finished cast's file pinned.
        self._activity_lock = threading.Lock()
        self._in_flight = 0
        self._last_activity = time.monotonic()
        self._bases: dict[str, float] = {}  # first segment → film time it starts at
        self._codecs: dict[str, str] = {}  # first segment → CODECS of its generation

    def handle_error(self, request, client_address) -> None:
        # Never print a traceback (the stdlib default) on the user's terminal: a TV
        # that hung up on seek is normal (urlproxy._Server does the same).
        exc = sys.exception()
        if not isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            _log.debug("serve: errore di richiesta (%s)", type(exc).__name__)

    def playlist_base(self, path: str) -> float:
        """Film time at which a live playlist's generation starts (its first segment's PTS;
        cached; 0 when unknown or from the very start)."""
        first = os.path.join(os.path.dirname(path), live.first_segment_name(os.path.basename(path)))
        with self._activity_lock:
            if first in self._bases:
                return self._bases[first]
        base = _start_pts(first)
        if base is None:
            return 0.0
        with self._activity_lock:
            self._bases[first] = base
        return base

    def sub_shift(self) -> float:
        """The live cast's subtitle shift in seconds: the manual `--sub-shift` total
        (`<live dir>/sub_shift`) plus an accepted after-start alignment (`align.json`)."""
        if not self.hls_dir:
            return 0.0
        try:
            with open(os.path.join(self.hls_dir, SUB_SHIFT), encoding="utf-8") as f:
                manual = float(f.read().strip() or 0.0)
        except (OSError, ValueError):
            manual = 0.0
        aligned = live.alignment(self.hls_dir).get("offset")
        return manual + (float(aligned) if isinstance(aligned, int | float) else 0.0)

    def generation_codecs(self, path: str) -> str:
        """`CODECS` for a master playlist, from its generation's first segment (cached)."""
        first = os.path.join(os.path.dirname(path), live.first_segment_name(os.path.basename(path)))
        key = "codecs:" + first
        with self._activity_lock:
            if key in self._codecs:
                return self._codecs[key]
        codecs = live.segment_codecs(first)
        if codecs:
            with self._activity_lock:
                self._codecs[key] = codecs
        return codecs

    def touch(self, delta: int) -> None:
        with self._activity_lock:
            self._in_flight += delta
            self._last_activity = time.monotonic()

    def idle_for(self) -> float:
        """Seconds since the last served request ended; 0 while one is in flight."""
        with self._activity_lock:
            return 0.0 if self._in_flight else time.monotonic() - self._last_activity


def _make_server(
    bind_ip: str,
    file_path: str | None,
    preferred_port: int = 0,
    token: str | None = None,
    sub_path: str | None = None,
    hls_dir: str | None = None,
    upstream: str | None = None,
    upstream_type: str = "",
    upstream_length: int | None = None,
    upstream_ranged: bool = False,
    media_name: str = "stream.mp4",
) -> _FileServer:
    """Bind a `_FileServer`, preferring the firewall-allowed cast port range so the receiver can
    actually reach us. `preferred_port > 0` forces that exact port; otherwise scan the range and
    fall back to an ephemeral port if it's fully busy."""

    def _bind(addr: tuple[str, int]) -> _FileServer:
        return _FileServer(
            addr, file_path, token=token, sub_path=sub_path, hls_dir=hls_dir,
            upstream=upstream, upstream_type=upstream_type,
            upstream_length=upstream_length, upstream_ranged=upstream_ranged,
            media_name=media_name,
        )  # fmt: skip

    if preferred_port:
        return _bind((bind_ip, preferred_port))
    for port in range(_CAST_PORT_LO, _CAST_PORT_HI + 1):
        try:
            return _bind((bind_ip, port))
        except OSError:
            continue
    _log.warning(
        "nessuna porta libera in %d-%d; uso una effimera (può servire una regola firewall)",
        _CAST_PORT_LO,
        _CAST_PORT_HI,
    )
    return _bind((bind_ip, 0))


def serve_file(
    file_path: str | None,
    bind_ip: str,
    sub_path: str | None = None,
    *,
    hls_dir: str | None = None,
    upstream: str | None = None,
    upstream_type: str = "",
    upstream_length: int | None = None,
    upstream_ranged: bool = False,
    media_name: str = "stream.mp4",
) -> tuple[_FileServer, int, threading.Thread]:
    """Start a threaded Range server for `file_path` (and/or a side-loaded WebVTT `sub_path`)
    bound to `bind_ip:0` (ephemeral port). Returns `(server, port, thread)`; the caller builds
    URLs via `served_url`/`served_sub_url(bind_ip, port, server.token)` and shuts down with
    `server.shutdown()`. The thread is a daemon (dies with the process), so this is the
    in-process (follow) mode — headless uses the `__main__` detached entrypoint.

    `upstream` (ADR 0045): Range-proxy a remote url on the media path instead of a file.
    The url is held in process memory — never on argv."""
    server = _make_server(
        bind_ip, file_path, sub_path=sub_path, hls_dir=hls_dir,
        upstream=upstream, upstream_type=upstream_type,
        upstream_length=upstream_length, upstream_ranged=upstream_ranged,
        media_name=media_name,
    )  # fmt: skip
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, name="nstream-serve", daemon=True)
    thread.start()
    return server, port, thread


def close_server(server: _FileServer) -> None:
    """follow-mode teardown: stop the thread and release the socket."""
    with contextlib.suppress(Exception):
        server.shutdown()
    with contextlib.suppress(Exception):
        server.server_close()


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = Path(base) / "nstream"
    with contextlib.suppress(OSError):
        d.mkdir(parents=True, exist_ok=True)
    return d


_ANNOUNCE_TIMEOUT = 10.0


def _readable_within(stream, timeout: float) -> bool:
    """Whether `stream` has data within `timeout`. A stream without a real descriptor
    (in-memory) is always readable."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return True
    return bool(select.select([fd], [], [], timeout)[0])


def spawn_detached(
    bind_ip: str,
    *,
    file_path: str | None = None,
    sub_path: str | None = None,
    hls_dir: str | None = None,
    job: live.Job | None = None,
    proxy: dict | None = None,
) -> tuple[int, int, str] | None:
    """Spawn a detached `python -m nstream.serve` serving `file_path` and/or a WebVTT `sub_path`
    on `bind_ip`, returning (pid, port, token) once it announces them, or None on failure. Detached
    (new session) so it outlives a headless return — the receiver fetches from it for the whole
    runtime. The per-cast URL token is generated by the server and read from its stdout (never on
    the command line, where `ps` would show it). Shared by `remux` (media) and `caster` (sub-only).

    With `hls_dir` + `job` the child also runs the live producer (ADR 0039): the job, which
    carries the debrid url, goes through its stdin — never its argv."""
    cmd = [sys.executable, "-m", "nstream.serve", "--bind", bind_ip]
    if file_path:
        cmd.append(file_path)
    if sub_path:
        cmd += ["--subs", sub_path]
    if hls_dir:
        cmd += ["--hls", hls_dir]
    if proxy and not job:
        cmd.append("--proxy")
    payload = json.dumps(job.to_dict() if job else (proxy or {})) + "\n" if (job or proxy) else None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        _log.warning("serve detach fallito: %s", e)
        return None
    if payload is not None and proc.stdin is not None:
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except OSError:
            kill_detached(proc.pid)
            return None
    if proc.stdout is None:
        kill_detached(proc.pid)
        return None
    # The child announces port and token right after binding; a child that hangs before
    # that (stuck import, full disk for its log) must not hang the cast forever.
    if not _readable_within(proc.stdout, _ANNOUNCE_TIMEOUT):
        _log.warning("serve: nessun annuncio entro %.0fs", _ANNOUNCE_TIMEOUT)
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


def _sub_server_state() -> util.RunState:
    # Runtime dir (tmpfs, per boot), not the cache: a pid that survived a reboot in
    # ~/.cache could name an unrelated process the next reap would SIGTERM. RunState also
    # records the process start time, so a reused pid within a boot reads back as None.
    return util.RunState("sub-server")


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
    _sub_server_state().write({"pid": pid, "persist": persist_path or ""})


def _proxy_server_state() -> util.RunState:
    return util.RunState("lan-proxy")


def register_proxy_server(pid: int) -> None:
    """Record the detached LAN Range-proxy (ADR 0045) so `--stop` / the next cast reaps it."""
    _proxy_server_state().write({"pid": pid})


_inproc_proxy_shutdown = None


def register_inproc_proxy(shutdown) -> None:
    """Track a follow-mode in-process LAN server left up because the receiver
    already has our content_id. Next cast / `--stop` in this process reaps it."""
    reap_inproc_proxy()
    global _inproc_proxy_shutdown
    _inproc_proxy_shutdown = shutdown


def reap_inproc_proxy() -> bool:
    """Shut a leftover in-process LAN proxy. True if there was one."""
    global _inproc_proxy_shutdown
    fn = _inproc_proxy_shutdown
    _inproc_proxy_shutdown = None
    if fn is None:
        return False
    cancel_reap(fn)
    with contextlib.suppress(Exception):
        fn()
    return True


_reap_live: set[object] = set()


def schedule_reap(
    fn,
    seconds: float,
    *,
    handle: object | None = None,
    skip_if=None,
    idle_for=None,
    poll: float | None = None,
) -> None:
    """Reap only `fn` (this server/pid). Daemon: process exit also ends it.

    `handle` is this leftover's identity; `cancel_reap(handle)` or a later
    `schedule_reap` with the same handle supersedes it. A different handle
    is a different server — this timer will not close it.

    When `idle_for` is set, wait until it reports ≥ `seconds` of HTTP idle
    (a playing HEVC cast keeps requesting). `skip_if()` True (receiver has
    our content) re-arms. Headless: the thread dies with the CLI; the
    detached child is bounded by `IDLE_EXIT_S` / `--stop`.
    """
    token: object = object() if handle is None else handle
    _reap_live.discard(token)
    _reap_live.add(token)
    if poll is not None:
        poll_s = poll
    elif seconds < 1:
        poll_s = 0.05
    else:
        poll_s = min(1.0, max(0.05, seconds / 4))

    def later() -> None:
        deadline = time.monotonic() + max(seconds, 0.0)
        while True:
            if token not in _reap_live:
                return
            if skip_if is not None and skip_if():
                deadline = time.monotonic() + max(seconds, 0.05)
                time.sleep(poll_s)
                continue
            if idle_for is not None:
                try:
                    idle = float(idle_for())
                except Exception:
                    idle = 0.0
                if idle >= seconds:
                    break
                time.sleep(poll_s)
                continue
            leftover = deadline - time.monotonic()
            if leftover <= 0:
                break
            time.sleep(min(poll_s, leftover))
        if token not in _reap_live:
            return
        if skip_if is not None and skip_if():
            schedule_reap(fn, seconds, handle=token, skip_if=skip_if, idle_for=idle_for, poll=poll)
            return
        _reap_live.discard(token)
        with contextlib.suppress(Exception):
            fn()

    if seconds <= 0 and idle_for is None and skip_if is None:
        if token in _reap_live:
            _reap_live.discard(token)
            with contextlib.suppress(Exception):
                fn()
        return
    threading.Thread(target=later, name="nstream-unconfirmed-reap", daemon=True).start()


def cancel_reap(handle: object) -> None:
    """Drop a pending owned reaper (a later cast replaced this server)."""
    _reap_live.discard(handle)


def reap_proxy_server() -> bool:
    """Kill and forget a leftover LAN Range-proxy (detached pid and/or in-process)."""
    found = reap_inproc_proxy()
    st = _proxy_server_state()
    data = st.read()
    if data is None:
        return found
    kill_detached(data.get("pid"))
    st.clear()
    return True


def reap_sub_server() -> bool:
    """Kill and forget a leftover standalone subtitle server, removing its persisted VTT
    copy. True if there was one (best-effort, idempotent)."""
    with contextlib.suppress(OSError):  # pre-1.39 location: may predate a reboot, never trusted
        _legacy_sub_state().unlink(missing_ok=True)
    st = _sub_server_state()
    data = st.read()
    if data is None:
        return False
    kill_detached(data.get("pid"))
    if data.get("persist"):
        with contextlib.suppress(OSError):
            os.unlink(data["persist"])
    st.clear()
    return True


def _legacy_sub_state() -> Path:
    return _cache_dir() / "sub-server.pid"


# A detached server with no served request for this long exits: the cast ended (or the TV
# was switched off) without a `--stop`, and the process would otherwise pin a multi-GB remux
# file forever (`remux._gc_stale` keeps any file whose server is alive). Long enough to
# survive a paused film; the receiver re-requests with Range on resume while it's up.
IDLE_EXIT_S = 3 * 3600.0


def _exit_when_idle(server: _FileServer, idle_s: float) -> None:
    while True:
        time.sleep(min(60.0, idle_s / 4))
        if server.idle_for() >= idle_s:
            _log.info("serve: inattivo da %.0fs → esco", idle_s)
            server.shutdown()
            return


RESTART_REQUEST = "restart.json"
SUB_SHIFT = "sub_shift"  # seconds the served WebVTT cues move (`--sub-shift`)


def _start_pts(segment: str) -> float | None:
    proc = util.run_cmd(
        ["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0", segment],
        timeout=10,
    )  # fmt: skip
    try:
        return float((proc.stdout or "").split()[0]) if proc and proc.returncode == 0 else None
    except (IndexError, ValueError):
        return None


def _restart_from_request(producer: live.Producer, hls_dir: str) -> None:
    try:
        with open(os.path.join(hls_dir, RESTART_REQUEST), encoding="utf-8") as f:
            req = json.load(f)
        producer.request_restart(float(req["ss"]), int(req["gen"]))
    except (OSError, ValueError, KeyError, TypeError) as e:
        _log.warning("serve: richiesta di riavvio live non valida (%s)", type(e).__name__)


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
    ap.add_argument("--hls")  # live HLS directory; the producer job arrives on stdin
    ap.add_argument("--proxy", action="store_true")  # remote url arrives on stdin (never argv)
    ap.add_argument("--idle-exit", type=float, default=IDLE_EXIT_S)
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
    if not args.file and not args.subs and not args.hls and not args.proxy:
        print("serve: né file né --subs né --hls né --proxy specificati", file=sys.stderr)
        return 2
    producer: live.Producer | None = None
    proxy_spec: dict | None = None
    if args.proxy:
        try:
            proxy_spec = json.loads(sys.stdin.readline())
            upstream = proxy_spec.get("upstream")
            if not isinstance(upstream, str) or not upstream.startswith(("http://", "https://")):
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            print("serve: job proxy non valido", file=sys.stderr)
            return 2
    if args.hls:
        try:
            job = live.Job.from_dict(json.loads(sys.stdin.readline()))
        except (ValueError, KeyError, TypeError):
            print("serve: job live non valido", file=sys.stderr)
            return 2
        producer = live.Producer.start(job, args.hls)
        if producer is None:
            return 2
    # SIGTERM (`--stop`, a new cast, GC) must run the cleanup below: stop the producer and
    # remove its segments, not just die.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if producer is not None:
        live_producer = producer

        def restart(*_: object) -> None:
            # Seek anywhere (`remux.live_seek`): the request is a 0600 file in the live dir
            # — `{"ss": film time, "gen": N}` — signalled with SIGUSR1. Run off the handler.
            threading.Thread(
                target=_restart_from_request, args=(live_producer, args.hls), daemon=True
            ).start()

        signal.signal(signal.SIGUSR1, restart)
    if proxy_spec is not None:
        length = proxy_spec.get("content_length")
        length_i = (
            int(length)
            if isinstance(length, int) or (isinstance(length, str) and length.isdigit())
            else None
        )
        server = _make_server(
            args.bind, args.file, preferred_port=args.port, sub_path=args.subs, hls_dir=args.hls,
            upstream=proxy_spec["upstream"],
            upstream_type=str(proxy_spec.get("content_type") or "video/mp4"),
            upstream_length=length_i,
            upstream_ranged=bool(proxy_spec.get("ranged")),
            media_name=str(proxy_spec.get("media_name") or "stream.mp4"),
        )  # fmt: skip
    else:
        server = _make_server(
            args.bind, args.file, preferred_port=args.port, sub_path=args.subs, hls_dir=args.hls
        )
    server.producer = producer
    if producer is not None:
        producer.run_pacing()
        live.Aligner(producer).run()
    port = server.server_address[1]
    # The parent reads exactly these two lines to learn port+token, then leaves us running.
    sys.stdout.write(f"PORT={port}\nTOKEN={server.token}\n")
    sys.stdout.flush()
    if args.idle_exit > 0:
        threading.Thread(target=_exit_when_idle, args=(server, args.idle_exit), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if producer is not None:
            producer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
