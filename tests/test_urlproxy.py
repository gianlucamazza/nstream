"""The loopback proxy keeps debrid tokens out of child argv while serving ranged reads."""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nstream import urlproxy

BODY = bytes(range(256)) * 64  # 16 KiB


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng:
            a, b = rng.removeprefix("bytes=").split("-")
            start, end = int(a), int(b or len(BODY) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
            chunk = BODY[start : end + 1]
        else:
            self.send_response(200)
            chunk = BODY
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(chunk)


def _serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_local_url_hides_upstream_and_forwards_ranges(monkeypatch):
    srv = _serve()
    try:
        upstream = f"http://127.0.0.1:{srv.server_address[1]}/realdebrid=SECRET/file.mkv"
        # The fake upstream is loopback; pretend it is remote to exercise the proxy.
        monkeypatch.setattr(urlproxy, "_is_local", lambda host: False)
        local = urlproxy.local_url(upstream)
        assert "SECRET" not in local and local.startswith("http://127.0.0.1:")
        req = urllib.request.Request(local, headers={"Range": "bytes=100-199"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 206
            assert r.headers["Content-Range"] == f"bytes 100-199/{len(BODY)}"
            assert r.read() == BODY[100:200]
    finally:
        srv.shutdown()


def test_unknown_route_is_404():
    host, port = urlproxy._ensure_server().server_address[:2]
    try:
        urllib.request.urlopen(f"http://{host}:{port}/nope", timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 404
    else:
        raise AssertionError("expected 404")


def test_loopback_and_non_http_urls_pass_through():
    assert urlproxy.local_url("http://127.0.0.1:8090/stream/x") == "http://127.0.0.1:8090/stream/x"
    assert urlproxy.local_url("/tmp/file.mp4") == "/tmp/file.mp4"


def test_is_remote_skips_lan_and_stubs():
    assert urlproxy.is_remote("https://debrid.example.com/realdebrid=SECRET/f.mp4")
    assert not urlproxy.is_remote("http://rd.example/f.mp4")  # RFC 2606 stub
    assert not urlproxy.is_remote("http://127.0.0.1:8090/stream/x")
    assert not urlproxy.is_remote("http://192.168.1.10:8090/f.mp4")
    assert not urlproxy.is_remote("http://10.0.0.5/f.mp4")
    assert not urlproxy.is_remote("http://u")  # caster test stub
    assert not urlproxy.is_remote("/tmp/file.mp4")
    assert not urlproxy.is_remote("http://192.0.2.10:45001/cast/tok/stream.mp4")
    assert not urlproxy.is_remote("http://100.64.0.1/f.mp4")  # CGNAT
    assert not urlproxy.is_remote("http://[fc00::1]/f.mp4")  # ULA
    assert not urlproxy.is_remote("http://[::7f00:1]/f.mp4")  # IPv4-compatible loopback


class _Dropping(_Upstream):
    """Sends half the body of a full GET, then drops the connection; ranged GETs work."""

    drops = 0

    def do_GET(self):
        if self.headers.get("Range") is None and _Dropping.drops == 0:
            _Dropping.drops += 1
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(BODY[: len(BODY) // 2])
            self.wfile.flush()
            self.connection.shutdown(2)
            self.close_connection = True
            return
        super().do_GET()


def test_upstream_drop_mid_body_is_resumed_with_a_range(monkeypatch):
    """A long read (the live producer reads a whole film) must survive a debrid drop: the
    proxy re-requests the rest from the last byte it delivered."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Dropping)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(urlproxy, "_is_local", lambda host: False)
        local = urlproxy.local_url(f"http://127.0.0.1:{srv.server_address[1]}/f.mkv")
        with urllib.request.urlopen(local, timeout=5) as r:
            assert r.read() == BODY
        assert _Dropping.drops == 1
    finally:
        srv.shutdown()


def test_unreachable_upstream_prints_no_traceback(monkeypatch, capfd):
    monkeypatch.setattr(urlproxy, "_is_local", lambda host: False)
    local = urlproxy.local_url("http://127.0.0.1:9/nothing-listens-here")
    try:
        urllib.request.urlopen(local, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 502
    assert "Traceback" not in capfd.readouterr().err


def test_blocked_ip_covers_cgnat_ula_and_v4_mapped():
    assert urlproxy._blocked_ip("127.0.0.1")
    assert urlproxy._blocked_ip("::1")
    assert urlproxy._blocked_ip("192.168.1.1")
    assert urlproxy._blocked_ip("169.254.1.1")
    assert urlproxy._blocked_ip("0.0.0.0")
    assert urlproxy._blocked_ip("100.64.0.1")
    assert urlproxy._blocked_ip("fc00::1")
    assert urlproxy._blocked_ip("::ffff:127.0.0.1")
    assert urlproxy._blocked_ip("::ffff:100.64.0.1")
    assert urlproxy._blocked_ip("::7f00:1")  # IPv4-compatible 127.0.0.1
    assert urlproxy._blocked_ip("::c0a8:1")  # IPv4-compatible 192.168.0.1
    assert not urlproxy._blocked_ip("8.8.8.8")
    assert not urlproxy._blocked_ip("2001:4860:4860::8888")
    assert not urlproxy._blocked_ip("debrid.example.com")


def _allow_loopback_peer(monkeypatch):
    """Fixture servers bind 127.0.0.1; allow that peer after a named-host pin."""
    real = urlproxy._blocked_ip
    monkeypatch.setattr(
        urlproxy, "_blocked_ip", lambda host: False if host in ("127.0.0.1", "::1") else real(host)
    )


def test_connect_pinned_skips_blocked_then_public(monkeypatch):
    """First getaddrinfo result is blocked; the second is the fixture and succeeds."""
    hits = {"n": 0}

    class _Count(_Upstream):
        def do_GET(self):
            hits["n"] += 1
            super().do_GET()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Count)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def fake_gai(host, port, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port or 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", srv.server_address[1])),
        ]

    monkeypatch.setattr(urlproxy.socket, "getaddrinfo", fake_gai)
    _allow_loopback_peer(monkeypatch)
    try:
        with urlproxy.open_upstream(
            f"http://dual.example.com:{port}/x", "GET", None, timeout=2
        ) as r:
            assert r.status == 200
            assert r.read() == BODY
        assert hits["n"] == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_connect_pinned_skips_unreachable_then_public(monkeypatch):
    """First public result refuses the connect; the second public address works."""
    hits = {"n": 0}

    class _Count(_Upstream):
        def do_GET(self):
            hits["n"] += 1
            super().do_GET()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Count)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def fake_gai(host, port, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 1)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", srv.server_address[1])),
        ]

    orig_connect = socket.socket.connect

    def fake_connect(self, address):
        if address[0] == "203.0.113.10":
            raise OSError("unreachable")
        return orig_connect(self, address)

    monkeypatch.setattr(urlproxy.socket, "getaddrinfo", fake_gai)
    monkeypatch.setattr(socket.socket, "connect", fake_connect)
    _allow_loopback_peer(monkeypatch)
    try:
        with urlproxy.open_upstream(
            f"http://dual.example.com:{port}/x", "GET", None, timeout=2
        ) as r:
            assert r.status == 200
            assert r.read() == BODY
        assert hits["n"] == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_pinned_opener_ignores_http_proxy_env(monkeypatch):
    """http_proxy must not steal the pinned hop (that would skip getaddrinfo pinning)."""
    origin_hits = {"n": 0}
    proxy_hits = {"n": 0}

    class _Origin(_Upstream):
        def do_GET(self):
            origin_hits["n"] += 1
            super().do_GET()

    class _Proxy(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def do_GET(self):
            proxy_hits["n"] += 1
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

    origin = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _Proxy)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    monkeypatch.setenv("http_proxy", proxy_url)
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("https_proxy", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)

    def fake_gai(host, port, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", origin.server_address[1]))
        ]

    monkeypatch.setattr(urlproxy.socket, "getaddrinfo", fake_gai)
    _allow_loopback_peer(monkeypatch)
    try:
        url = f"http://cdn.example.com:{origin.server_address[1]}/file"
        with urlproxy.open_upstream(url, "GET", None, timeout=2) as resp:
            assert resp.status == 200
            assert resp.read() == BODY
        assert origin_hits["n"] == 1
        assert proxy_hits["n"] == 0
    finally:
        origin.shutdown()
        origin.server_close()
        proxy.shutdown()
        proxy.server_close()


def _hang_connect(monkeypatch, hanging: set[str]):
    """`connect` to `hanging` hosts sleeps for the socket timeout, then times out.

    Models a blackhole (SYN with no RST). `settimeout` is honoured so the overall
    deadline tests do not depend on a real unroutable network.
    """
    real_connect = socket.socket.connect

    def connect(self, address):
        host = address[0] if address else ""
        if host in hanging:
            t = self.gettimeout()
            time.sleep(60.0 if t is None else max(0.0, float(t)))
            raise TimeoutError("timed out")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


def test_interleave_families_alternates_and_caps():
    v6 = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (f"2001:db8::{i}", 80, 0, 0)) for i in range(4)
    ]
    v4 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"203.0.113.{i}", 80)) for i in range(3)]
    out = urlproxy._interleave_families([*v6, *v4])
    assert [row[0] for row in out] == [
        socket.AF_INET6,
        socket.AF_INET,
        socket.AF_INET6,
        socket.AF_INET,
    ]
    assert len(out) == 4  # 2 per family


def test_connect_pinned_overall_deadline(monkeypatch):
    """Three blackholed public A records must not cost N×timeout."""
    ips = ("8.8.8.8", "1.1.1.1", "9.9.9.9")

    def fake_gai(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80)) for ip in ips]

    monkeypatch.setattr(urlproxy.socket, "getaddrinfo", fake_gai)
    _hang_connect(monkeypatch, set(ips))
    t0 = time.monotonic()
    with pytest.raises(urllib.error.URLError):
        urlproxy.open_upstream("http://blackhole.example.com/x", "GET", None, timeout=0.3)
    elapsed = time.monotonic() - t0
    assert 0.25 <= elapsed <= 0.6  # one deadline, not 3×0.3 ≈ 0.9


def test_probe_stays_within_budget_on_blackholes(monkeypatch):
    """HEAD's connect deadline is `per`, not N×per — probe stays inside its budget."""
    ips = ("8.8.4.4", "1.0.0.1", "9.9.9.10")

    def fake_gai(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80)) for ip in ips]

    monkeypatch.setattr(urlproxy.socket, "getaddrinfo", fake_gai)
    _hang_connect(monkeypatch, set(ips))
    urlproxy._probe_cache.clear()
    t0 = time.monotonic()
    assert urlproxy.probe("http://blackhole.example.com/x", timeout=0.5).ok is False
    elapsed = time.monotonic() - t0
    assert elapsed < 0.8  # budget 0.5 plus slack; not 3×0.5


def test_connect_pinned_interleaves_hanging_ipv6_then_ipv4(monkeypatch):
    """A hanging AAAA first must not starve a working A: success within one slot."""
    hits = {"n": 0}

    class _Count(_Upstream):
        def do_GET(self):
            hits["n"] += 1
            super().do_GET()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Count)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def fake_gai(host, port, *a, **k):
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2001:4860:4860::8888", port or 80, 0, 0),
            ),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2606:4700:4700::1111", port or 80, 0, 0),
            ),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", srv.server_address[1])),
        ]

    monkeypatch.setattr(urlproxy.socket, "getaddrinfo", fake_gai)
    _hang_connect(monkeypatch, {"2001:4860:4860::8888", "2606:4700:4700::1111"})
    _allow_loopback_peer(monkeypatch)
    t0 = time.monotonic()
    try:
        with urlproxy.open_upstream(
            f"http://dual.example.com:{port}/x", "GET", None, timeout=1.0
        ) as r:
            assert r.status == 200
            assert r.read() == BODY
        elapsed = time.monotonic() - t0
        assert hits["n"] == 1
        assert elapsed < 1.0  # one slot; IPv4 starts after the family delay
        sock = urlproxy._connect_pinned("dual.example.com", port, 1.0)
        try:
            assert sock.gettimeout() == 1.0  # read timeout restored, not the CAD slot
        finally:
            sock.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_hostname_resolving_to_loopback_is_refused(monkeypatch):
    monkeypatch.setattr(
        urlproxy.socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 80))
        ],
    )
    urlproxy._probe_cache.clear()
    with pytest.raises(urllib.error.URLError):
        urlproxy.open_upstream("http://evil.example.com/x", "GET", None)
    assert urlproxy.probe("http://evil.example.com/x").ok is False


def test_dns_rebinding_public_then_loopback_is_refused(monkeypatch):
    """getaddrinfo returns a public IP first and 127.0.0.1 on the second call.

    The local fixture must not see a request: we pin the first resolve (or abort
    on getpeername) instead of letting urllib connect to the rebound loopback.
    """
    hits = {"n": 0}

    class _Count(_Upstream):
        def do_GET(self):
            hits["n"] += 1
            super().do_GET()

        def do_HEAD(self):
            hits["n"] += 1
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Count)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    n = {"gai": 0}

    def fake_gai(host, port, *a, **k):
        n["gai"] += 1
        ip = "203.0.113.10" if n["gai"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or srv.server_address[1]))]

    monkeypatch.setattr(urlproxy.socket, "getaddrinfo", fake_gai)
    url = f"http://rebinder.example.com:{srv.server_address[1]}/secret"
    urlproxy._probe_cache.clear()
    try:
        with pytest.raises(urllib.error.URLError):
            urlproxy.open_upstream(url, "GET", None, timeout=0.4)
        assert urlproxy.probe(url, timeout=0.4).ok is False
        assert hits["n"] == 0
    finally:
        srv.shutdown()
        srv.server_close()
        urlproxy._probe_cache.clear()
