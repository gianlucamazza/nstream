"""Loopback proxy that keeps debrid tokens out of child-process argv.

A debrid stream url carries the account token in its path, and `/proc/<pid>/cmdline` is
readable by every local user: handing it to `ffmpeg`/`ffprobe` as an argument published the
token for the whole run (seen in `pgrep -a ffmpeg`, 2026-10-01). `local_url(url)` registers
the url under a random path on an in-process server bound to 127.0.0.1 and returns that
loopback url instead; requests (Range included) are forwarded upstream and streamed back.

Only for children this process waits on (ffprobe, the remux ffmpeg, subtitle alignment):
the server lives as long as nstream does. Loopback urls (TorrServer, our own servers) are
returned unchanged — they carry no token and gain nothing from a second hop.

ADR 0045 Phase 1 also uses this module's probe / plan / open helpers so `serve` can
Range-serve the same remote url on the LAN (the TV never sees the debrid host). The
loopback server below stays 127.0.0.1-only; the LAN bind lives in `serve`.

`open_upstream` / `probe` pin each hop (first included) to one `getaddrinfo` result:
private / loopback / link-local / unspecified / CGNAT `100.64.0.0/10` / ULA `fc00::/7`
(and IPv4-mapped forms) are rejected, then the socket connects to that address.
The URL hostname stays on `Host`, SNI, and certificate verification. After connect,
`getpeername()` is checked again so a public-then-private rebind cannot land.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", re.I)
# HEAD + optional 1-byte Range GET share this budget (well under a 16 s stall).
_PROBE_BUDGET = 8.0
_PROBE_TIMEOUT = 4.0

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
        return open_upstream(upstream, method, rng)

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
            up_range = None
            if resp.status == 206:
                up_range = range_from_content_range(resp.headers.get("Content-Range"))
                if up_range is None and is_start_end_range(rng):
                    up_range = rng
            copy_body(
                resp, self.wfile.write, upstream, up_range,
                resumable and (up_range is not None or resp.status != 206), expected,
            )  # fmt: skip


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


# CGNAT (RFC 6598). Not covered by ipaddress.is_private.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Literal IPv4/IPv6, or None. Unwraps v4-mapped so `::ffff:127.0.0.1` is loopback."""
    raw = (host or "").split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _blocked_ip(host: str) -> bool:
    """True for private, loopback, link-local, unspecified, CGNAT, ULA (via is_private)."""
    addr = _parse_ip(host)
    if addr is None:
        return False
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return True
    return addr.version == 4 and addr in _CGNAT


def _assert_peer_public(sock: socket.socket) -> None:
    peer = sock.getpeername()[0]
    if _blocked_ip(str(peer)):
        raise urllib.error.URLError("blocked address")


def _connect_pinned(host: str, port: int, timeout, source_address=None):
    """Resolve `host` once, reject a blocked address, connect to that sockaddr.

    A literal IP in the URL is used as-is (loopback fixtures / TorrServer). A name
    must resolve to a public address; `getpeername()` must still be public.
    """
    port = int(port or 0)
    if _parse_ip(host) is not None:
        sock = socket.create_connection((host, port), timeout, source_address)
        return sock
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise urllib.error.URLError(exc) from exc
    if not infos:
        raise urllib.error.URLError("blocked address")
    fam, socktype, proto, _canon, sockaddr = infos[0]
    ip = sockaddr[0]
    if not isinstance(ip, str) or _blocked_ip(ip):
        raise urllib.error.URLError("blocked address")
    sock = socket.socket(fam, socktype, proto)
    if timeout is not None:
        sock.settimeout(timeout)
    try:
        if source_address:
            sock.bind(source_address)
        sock.connect(sockaddr)
        _assert_peer_public(sock)
    except Exception:
        sock.close()
        raise
    return sock


def _pinned_create(address, timeout, source_address):
    return _connect_pinned(address[0], address[1], timeout, source_address)


class _HTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that pins the peer to one resolved address (no DNS rebind)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # HTTPConnection.__init__ stores socket.create_connection on the instance.
        self._create_connection = _pinned_create


class _HTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection: pin the TCP peer; SNI / cert verify still use `self.host`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _pinned_create


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=getattr(self, "_context", None))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow 3xx only to http(s). Reject literal private/loopback/link-local targets
    and https→http downgrades. Named hosts are pinned at connect (`_connect_pinned`)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _guard_redirect(req.full_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect, _HTTPHandler, _HTTPSHandler)


def _guard_redirect(src_url: str, dst_url: str) -> None:
    src = urllib.parse.urlsplit(src_url)
    dst = urllib.parse.urlsplit(dst_url)
    if dst.scheme not in ("http", "https"):
        raise urllib.error.URLError("redirect scheme")
    if src.scheme == "https" and dst.scheme != "https":
        raise urllib.error.URLError("redirect downgrade")
    host = (dst.hostname or "").lower()
    if not host or _is_local(host) or host.endswith(".localhost"):
        raise urllib.error.URLError("redirect to private")
    if _blocked_ip(host):
        raise urllib.error.URLError("redirect to private")


def open_upstream(url: str, method: str, rng: str | None, timeout: float = _UPSTREAM_TIMEOUT):
    """GET/HEAD `url`, optionally with a Range. Never logs the url (it may carry a token).

    Every hop (first included) uses a pinned HTTP(S) connection: one `getaddrinfo`,
    reject blocked nets, connect to that address, then `getpeername()` abort. The
    URL hostname stays on Host / SNI / certificate verification. Redirects: no
    https→http and no literal private/loopback/link-local targets."""
    headers = {"User-Agent": UA}
    if rng:
        headers["Range"] = rng
    req = urllib.request.Request(url, headers=headers, method=method)
    return _opener.open(req, timeout=timeout)  # noqa: S310


def is_start_end_range(rng: str | None) -> bool:
    """True for `bytes=start-` / `bytes=start-end`. False for a suffix (`bytes=-N`)."""
    return bool(rng and _RANGE.match(rng))


def range_from_content_range(header: str | None) -> str | None:
    """Normalize a `Content-Range` into `bytes=start-end` for resume. None if absent/unparsed.

    A client suffix (`bytes=-N`) must not be replayed as `bytes=0+sent-`: the start is
    the upstream's Content-Range first byte."""
    if not header:
        return None
    m = _CONTENT_RANGE.match(str(header).strip())
    if not m:
        return None
    return f"bytes={m.group(1)}-{m.group(2)}"


def _resume_spec(rng: str | None) -> tuple[int, str] | None:
    """`(first, last)` from a start-end Range. None for suffix/malformed — do not guess 0."""
    m = _RANGE.match(rng or "")
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def copy_body(
    resp, write, upstream: str, rng: str | None, resumable: bool, expected: int | None
) -> None:
    """Stream `resp` to `write`; on an upstream drop, re-request the rest from the last byte
    delivered. A drop is an error OR an early EOF: http.client returns b"" short of the
    Content-Length instead of raising. `rng` must be a start-end Range (normalize suffix
    ranges via `range_from_content_range` first). Client-gone writes are silent."""
    parsed = _resume_spec(rng)
    if parsed is None:
        # Suffix / missing: do not guess start=0 on resume.
        first, last = 0, ""
        if rng:
            resumable = False
    else:
        first, last = parsed
    sent = 0
    tries = 0
    while True:
        try:
            if resp is None:  # reopen the rest after a drop
                resp = open_upstream(upstream, "GET", f"bytes={first + sent}-{last}")
                if resp.status != 206:  # the upstream ignored the Range: cannot splice
                    resp.close()
                    return
            chunk = resp.read(_CHUNK)
            if not chunk and expected is not None and sent < expected:
                raise http.client.IncompleteRead(b"", expected - sent)
        except _CLIENT_GONE:
            if resp is not None:
                resp.close()
            return
        except (OSError, http.client.HTTPException) as e:
            if resp is not None:
                resp.close()
                resp = None
            if not resumable or tries >= _RESUME_TRIES:
                _log.warning("proxy: upstream interrotto (%s)", type(e).__name__)
                return
            tries += 1
            continue
        if not chunk:
            resp.close()
            return
        try:
            write(chunk)
        except _CLIENT_GONE:
            resp.close()
            return
        sent += len(chunk)


def discard(resp, n: int) -> int:
    """Read and drop `n` bytes from `resp`. Returns how many were actually skipped."""
    skipped = 0
    while skipped < n:
        chunk = resp.read(min(_CHUNK, n - skipped))
        if not chunk:
            break
        skipped += len(chunk)
    return skipped


def copy_n(resp, write, n: int) -> int:
    """Copy exactly `n` bytes (or until EOF) from `resp` to `write`. Client-gone is silent."""
    sent = 0
    while sent < n:
        chunk = resp.read(min(_CHUNK, n - sent))
        if not chunk:
            break
        try:
            write(chunk)
        except _CLIENT_GONE:
            return sent
        sent += len(chunk)
    return sent


def is_remote(url: str) -> bool:
    """True when `url` is an http(s) resource the TV would pull over the WAN.

    Loopback, RFC1918, CGNAT, ULA, link-local, unspecified, our own `/cast/`
    capability paths, and single-label hosts (test stubs like `http://u`) stay
    as-is — they are already on the LAN or are not a real remote stream."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host or _is_local(host) or _blocked_ip(host):
        return False
    if host.endswith((".example", ".test", ".invalid", ".localhost")):
        return False  # RFC 2606 / test stubs (http://rd.example/…)
    if parts.path.startswith("/cast/"):
        return False
    return "." in host or ":" in host


@dataclass(frozen=True)
class Probe:
    """What a HEAD / Range probe learned about an upstream, without logging the url."""

    ok: bool
    content_type: str = ""
    content_length: int | None = None
    ranged: bool = False


_probe_cache: dict[str, Probe] = {}


@dataclass(frozen=True)
class LanPlan:
    """How to hand a remote stream to a DMR (ADR 0045 Phase 1).

    `proxy`: LAN Range-serve the remote bytes, video untouched. Requires a successful
    probe, a known length, and an upstream that honours Range.
    `rewrap`: container / `hev1` tag is not DMR-loadable — remux `-c copy` to MP4
    (ADR 0022), not a 720 H.264 transcode.
    `cache`: no Range (or unknown length) — not proxyable; `cast_flow` routes to the
    disk-capped remux / live tier.
    `fail`: probe failed; do not WAN-direct and do not advertise Accept-Ranges.
    """

    mode: str
    content_type: str
    content_length: int | None
    ranged: bool
    media_name: str
    reason: str


def _ctype(headers) -> str:
    return (headers.get("Content-Type") or "").split(";", 1)[0].strip()


def _int_len(headers) -> int | None:
    raw = headers.get("Content-Length") if headers is not None else None
    if raw and str(raw).isdigit():
        return int(raw)
    return None


def _accepts_ranges(headers) -> bool:
    return (headers.get("Accept-Ranges") or "").lower() == "bytes"


def _length_from_range(headers) -> int | None:
    raw = headers.get("Content-Range") if headers is not None else None
    if not raw:
        return None
    m = _CONTENT_RANGE.match(str(raw).strip())
    if not m or m.group(3) == "*":
        return None
    return int(m.group(3))


def _declared_mime(raw: str) -> str:
    base = raw.split(";", 1)[0].strip().lower()
    return base if base in ("video/mp4", "video/webm") else ""


def _probe_range(url: str, timeout: float) -> tuple[bool, int | None]:
    """A 1-byte GET tells us whether the upstream honours Range even when HEAD omitted
    Accept-Ranges. The body is never consumed on a 200 (the server ignored Range)."""
    try:
        resp = open_upstream(url, "GET", "bytes=0-0", timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            if e.code == 206:
                return True, _length_from_range(e.headers) or _int_len(e.headers)
            return False, _int_len(e.headers)
        finally:
            e.close()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False, None
    with resp:
        if resp.status == 206:
            return True, _length_from_range(resp.headers) or _int_len(resp.headers)
        return False, _int_len(resp.headers)


def probe(url: str, timeout: float = _PROBE_BUDGET) -> Probe:
    """HEAD (then a 1-byte Range GET if needed) of `url`. Never logs the url.

    Uses `open_upstream` (pinned resolve on every hop, first included). Total wait
    is `timeout` (default 8 s) split across HEAD + Range GET — well under a 16 s
    stall. Memoized per url so `cast_flow` + `lan_media` share one probe."""
    hit = _probe_cache.get(url)
    if hit is not None:
        return hit
    deadline = time.monotonic() + max(0.1, timeout)
    per = min(_PROBE_TIMEOUT, timeout)
    ct, cl, ranged, ok = "", None, False, False
    try:
        resp = open_upstream(url, "HEAD", None, timeout=per)
    except urllib.error.HTTPError as e:
        try:
            ok = e.code < 500
            ct = _ctype(e.headers)
            cl = _int_len(e.headers) or _length_from_range(e.headers)
            ranged = e.code == 206 or _accepts_ranges(e.headers)
        finally:
            e.close()
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        _log.warning("probe: upstream irraggiungibile (%s)", type(e).__name__)
        result = Probe(False)
        _probe_cache[url] = result
        return result
    else:
        with resp:
            ok = True
            ct = _ctype(resp.headers)
            cl = _int_len(resp.headers) or _length_from_range(resp.headers)
            ranged = resp.status == 206 or _accepts_ranges(resp.headers)
    leftover = deadline - time.monotonic()
    if ok and not ranged and leftover > 0:
        hit_r, total = _probe_range(url, min(per, leftover))
        ranged = hit_r
        cl = cl or total
    result = Probe(ok, ct, cl, ranged)
    if len(_probe_cache) > 16:
        _probe_cache.clear()
    _probe_cache[url] = result
    return result


def plan(container: str, probe: Probe, video_codec: str = "", codec_tag: str = "") -> LanPlan:
    """Choose LAN proxy vs remux/live. Video stays native (ADR 0017 / 0045).

    Never proxies when the probe failed, the length is unknown, or the upstream does
    not honour Range (no synthesized 206). Matroska / `hev1` → `rewrap` (`-c copy` to
    MP4, ADR 0022). A known undecodable video codec is still `proxy` (native bytes):
    remux-720 is not a product answer. Never advertise Accept-Ranges we cannot honour."""
    from . import quality

    if not probe.ok:
        return LanPlan("fail", "", None, False, "stream.mp4", "lan_probe_failed")
    mime = quality.container_mime(container)
    if container and not quality.container_castable(container):
        return LanPlan(
            "rewrap", "video/mp4", probe.content_length, probe.ranged, "stream.mp4", "container"
        )
    if (codec_tag or "").lower() == "hev1":
        return LanPlan(
            "rewrap", "video/mp4", probe.content_length, probe.ranged, "stream.mp4", "hev1"
        )
    ct = mime or _declared_mime(probe.content_type) or "video/mp4"
    name = "stream.webm" if container == "webm" or ct == "video/webm" else "stream.mp4"
    if not probe.ranged:
        return LanPlan("cache", ct, probe.content_length, False, name, "lan_no_range")
    if probe.content_length is None:
        return LanPlan("cache", ct, None, False, name, "lan_unknown_length")
    reason = "lan"
    if video_codec and video_codec not in quality.CAST_VIDEO_DECODABLE:
        reason = "lan_undecodable_video"
    return LanPlan("proxy", ct, probe.content_length, True, name, reason)


def proxy_job(url: str, lan: LanPlan) -> dict:
    """stdin payload for a detached `python -m nstream.serve --proxy` (never argv)."""
    return {
        "upstream": url,
        "content_type": lan.content_type,
        "content_length": lan.content_length,
        "ranged": lan.ranged,
        "media_name": lan.media_name,
    }
