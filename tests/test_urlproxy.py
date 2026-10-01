"""The loopback proxy keeps debrid tokens out of child argv while serving ranged reads."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
