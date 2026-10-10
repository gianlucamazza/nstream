"""ADR 0045 Phase 1: LAN Range-proxy of a remote url (native video, DMR 206 semantics).

A local fixture HTTP server stands in for a debrid host and serves known MP4 / MKV
bytes in four modes: Range-advertising and no-Range, each container. The nstream
serve proxy is pointed at that fixture; tests assert headers, 206, byte correctness,
and urlproxy.plan() (proxy vs remux `-c copy` rewrap). No mocks-as-done.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nstream import serve, urlproxy
from nstream.config import Config

MP4 = b"ftyp" + bytes(range(256)) * 32  # 8 KiB + 4, recognisable prefix
MKV = b"\x1a\x45\xdf\xa3" + bytes(range(256)) * 32  # EBML-ish + payload
SECRET = "realdebrid=SECRETTOKEN"


class _FixServer(ThreadingHTTPServer):
    mode: str = ""


class _Fixture(BaseHTTPRequestHandler):
    """Configurable upstream: `server.mode` is range-mp4 | norange-mp4 | range-mkv | norange-mkv."""

    protocol_version = "HTTP/1.1"
    server: _FixServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _body(self) -> bytes:
        return MKV if "mkv" in self.server.mode else MP4

    def _ctype(self) -> str:
        return "video/x-matroska" if "mkv" in self.server.mode else "video/mp4"

    def _ranged(self) -> bool:
        return self.server.mode.startswith("range-")

    def do_HEAD(self) -> None:
        self._reply(write_body=False)

    def do_GET(self) -> None:
        self._reply(write_body=True)

    def _reply(self, *, write_body: bool) -> None:
        body = self._body()
        rng = self.headers.get("Range")
        if self._ranged() and rng and rng.startswith("bytes="):
            spec = rng[len("bytes=") :]
            first, _, last = spec.partition("-")
            start = int(first) if first else 0
            end = int(last) if last else len(body) - 1
            end = min(end, len(body) - 1)
            chunk = body[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Accept-Ranges", "bytes")
        else:
            chunk = body
            self.send_response(200)
            if self._ranged():
                self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", self._ctype())
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        if write_body:
            self.wfile.write(chunk)


def _start_fixture(mode: str) -> _FixServer:
    srv = _FixServer(("127.0.0.1", 0), _Fixture)
    srv.mode = mode
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _upstream(srv: _FixServer, name: str = "file.mp4") -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/{SECRET}/{name}"


def _proxy(
    upstream: str, *, content_type: str, length: int | None, ranged: bool, name: str = "stream.mp4"
):
    server, port, _thread = serve.serve_file(
        None, "127.0.0.1",
        upstream=upstream, upstream_type=content_type,
        upstream_length=length, upstream_ranged=ranged, media_name=name,
    )  # fmt: skip
    url = serve.served_url("127.0.0.1", port, server.token, name)
    return server, url


@pytest.mark.parametrize(
    ("mode", "container", "body", "want_mode", "want_type"),
    [
        ("range-mp4", "mp4", MP4, "proxy", "video/mp4"),
        ("norange-mp4", "mp4", MP4, "proxy", "video/mp4"),
        ("range-mkv", "mkv", MKV, "rewrap", "video/mp4"),
        ("norange-mkv", "mkv", MKV, "rewrap", "video/mp4"),
    ],
)
def test_plan_is_container_aware(mode, container, body, want_mode, want_type):
    srv = _start_fixture(mode)
    try:
        probed = urlproxy.probe(_upstream(srv, "f.mkv" if container == "mkv" else "f.mp4"))
        assert probed.ok
        assert probed.content_length == len(body)
        assert probed.ranged is mode.startswith("range-")
        planned = urlproxy.plan(container, probed, video_codec="hevc")
        assert planned.mode == want_mode
        assert planned.content_type == want_type
        if want_mode == "proxy":
            assert planned.media_name == "stream.mp4"
            assert planned.reason.startswith("lan")
    finally:
        srv.shutdown()


def test_plan_keeps_undecodable_video_native():
    """ADR 0017 already failed/mirrored mpeg4; Phase 1 must not invent remux-720."""
    planned = urlproxy.plan(
        "mp4", urlproxy.Probe(True, "video/mp4", 100, True), video_codec="mpeg4"
    )
    assert planned.mode == "proxy"
    assert planned.content_type == "video/mp4"
    assert planned.reason == "lan_undecodable_video"


def test_plan_webm_declares_webm_mime():
    planned = urlproxy.plan("webm", urlproxy.Probe(True, "application/octet-stream", 10, True))
    assert planned.mode == "proxy"
    assert planned.content_type == "video/webm"
    assert planned.media_name == "stream.webm"


@pytest.mark.parametrize("mode", ["range-mp4", "norange-mp4"])
def test_proxy_range_get_is_206_with_correct_bytes(mode):
    fixture = _start_fixture(mode)
    try:
        upstream = _upstream(fixture)
        probed = urlproxy.probe(upstream)
        planned = urlproxy.plan("mp4", probed, "hevc")
        assert planned.mode == "proxy"
        server, url = _proxy(
            upstream, content_type=planned.content_type,
            length=planned.content_length, ranged=planned.ranged,
        )  # fmt: skip
        try:
            assert SECRET not in url
            req = urllib.request.Request(url, headers={"Range": "bytes=100-199"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 206
                assert resp.headers["Content-Type"] == "video/mp4"
                assert resp.headers["Accept-Ranges"] == "bytes"
                assert resp.headers["Content-Length"] == "100"
                assert resp.headers["Content-Range"] == f"bytes 100-199/{len(MP4)}"
                assert resp.read() == MP4[100:200]
        finally:
            server.shutdown()
    finally:
        fixture.shutdown()


@pytest.mark.parametrize("mode", ["range-mp4", "norange-mp4"])
def test_proxy_head_declares_length_and_ranges(mode):
    fixture = _start_fixture(mode)
    try:
        upstream = _upstream(fixture)
        probed = urlproxy.probe(upstream)
        server, url = _proxy(
            upstream, content_type="video/mp4",
            length=probed.content_length, ranged=probed.ranged,
        )  # fmt: skip
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "video/mp4"
                assert resp.headers["Accept-Ranges"] == "bytes"
                assert resp.headers["Content-Length"] == str(len(MP4))
                assert resp.read() == b""
        finally:
            server.shutdown()
    finally:
        fixture.shutdown()


def test_proxy_suffix_and_open_range():
    fixture = _start_fixture("range-mp4")
    try:
        upstream = _upstream(fixture)
        server, url = _proxy(upstream, content_type="video/mp4", length=len(MP4), ranged=True)
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=100-"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 206
                assert resp.read() == MP4[100:]
            req = urllib.request.Request(url, headers={"Range": f"bytes=-{16}"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 206
                assert resp.read() == MP4[-16:]
        finally:
            server.shutdown()
    finally:
        fixture.shutdown()


def test_proxy_unsatisfiable_range_is_416():
    fixture = _start_fixture("norange-mp4")
    try:
        upstream = _upstream(fixture)
        server, url = _proxy(upstream, content_type="video/mp4", length=len(MP4), ranged=False)
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={len(MP4)}-"})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 416
            assert exc.value.headers["Content-Range"] == f"bytes */{len(MP4)}"
        finally:
            server.shutdown()
    finally:
        fixture.shutdown()


def test_proxy_wrong_path_is_404_and_hides_secret():
    fixture = _start_fixture("range-mp4")
    try:
        upstream = _upstream(fixture)
        server, url = _proxy(upstream, content_type="video/mp4", length=len(MP4), ranged=True)
        try:
            assert SECRET not in url
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/stream.mp4", timeout=5
                )
            assert exc.value.code == 404
        finally:
            server.shutdown()
    finally:
        fixture.shutdown()


def test_mkv_is_rewrap_not_proxied_as_matroska():
    """The DMR refuses Matroska (ADR 0022). Phase 1 must choose `-c copy` to MP4,
    not pass the mkv through and not remux-720."""
    fixture = _start_fixture("range-mkv")
    try:
        probed = urlproxy.probe(_upstream(fixture, "title.mkv"))
        planned = urlproxy.plan("mkv", probed, "hevc")
        assert planned.mode == "rewrap"
        assert planned.content_type == "video/mp4"
        assert planned.media_name == "stream.mp4"
        assert planned.reason == "container"
    finally:
        fixture.shutdown()


def test_detached_proxy_announces_and_serves_without_url_on_argv(tmp_path):
    fixture = _start_fixture("range-mp4")
    try:
        upstream = _upstream(fixture)
        planned = urlproxy.plan("mp4", urlproxy.probe(upstream), "hevc")
        job = urlproxy.proxy_job(upstream, planned)
        proc = subprocess.Popen(
            [sys.executable, "-m", "nstream.serve", "--bind", "127.0.0.1", "--proxy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps(job) + "\n")
            proc.stdin.close()
            port_line = proc.stdout.readline().strip()
            token_line = proc.stdout.readline().strip()
            assert port_line.startswith("PORT=") and token_line.startswith("TOKEN=")
            # The debrid url must not appear on argv (the reason urlproxy exists).
            argv = proc.args
            shown = " ".join(str(a) for a in argv) if isinstance(argv, list) else str(argv)
            assert SECRET not in shown
            port, token = int(port_line[len("PORT=") :]), token_line[len("TOKEN=") :]
            url = serve.served_url("127.0.0.1", port, token, planned.media_name)
            assert SECRET not in url
            req = urllib.request.Request(url, headers={"Range": "bytes=0-3"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 206
                assert resp.headers["Content-Type"] == "video/mp4"
                assert resp.read() == MP4[:4]
        finally:
            proc.kill()
            proc.wait()
    finally:
        fixture.shutdown()


def test_lan_media_rewrites_remote_url(monkeypatch):
    from nstream import caster

    fixture = _start_fixture("range-mp4")
    try:
        # Pretend the fixture is a public debrid host so is_remote() accepts it.
        monkeypatch.setattr(urlproxy, "is_remote", lambda url: True)
        cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
        upstream = _upstream(fixture)
        lan = caster.lan_media(cfg, upstream, "127.0.0.1", container="mp4", video_codec="hevc")
        assert lan is not None
        try:
            assert SECRET not in lan.url
            assert lan.content_type == "video/mp4"
            assert lan.plan.mode == "proxy"
            req = urllib.request.Request(lan.url, headers={"Range": "bytes=4-7"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 206
                assert resp.read() == MP4[4:8]
        finally:
            if lan.shutdown:
                lan.shutdown()
    finally:
        fixture.shutdown()


def test_lan_media_off_returns_none():
    from nstream import caster

    cfg = Config(torrentio_base="tb", cast_lan_proxy=False)
    assert caster.lan_media(cfg, "https://debrid.example/f.mp4", "127.0.0.1") is None


def test_reap_proxy_server(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(serve, "kill_detached", lambda pid: None)
    serve.register_proxy_server(4242)
    assert serve.reap_proxy_server() is True
    assert serve.reap_proxy_server() is False
