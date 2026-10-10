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
    drops_left: int = 0
    drop_after: int = 50
    location: str = ""
    omit_length: bool = False
    ranges: list[str]


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
        return self.server.mode.startswith("range-") or self.server.mode.startswith("drop-")

    def do_HEAD(self) -> None:
        self._reply(write_body=False)

    def do_GET(self) -> None:
        self._reply(write_body=True)

    def _reply(self, *, write_body: bool) -> None:
        if self.server.location:
            self.send_response(302)
            self.send_header("Location", self.server.location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self._body()
        rng = self.headers.get("Range")
        self.server.ranges.append(rng or "")
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
        if self.server.omit_length:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if write_body:
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n0\r\n\r\n")
            return
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        if write_body:
            if self.server.drops_left > 0:
                self.server.drops_left -= 1
                self.wfile.write(chunk[: self.server.drop_after])
                self.close_connection = True
                return
            self.wfile.write(chunk)


def _start_fixture(mode: str, **kw) -> _FixServer:
    srv = _FixServer(("127.0.0.1", 0), _Fixture)
    srv.mode = mode
    srv.drops_left = int(kw.get("drops_left", 0))
    srv.drop_after = int(kw.get("drop_after", 50))
    srv.location = str(kw.get("location", ""))
    srv.omit_length = bool(kw.get("omit_length", False))
    srv.ranges = []
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
        ("norange-mp4", "mp4", MP4, "cache", "video/mp4"),
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
        if want_mode == "cache":
            assert planned.reason == "lan_no_range"
            assert planned.ranged is False
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


@pytest.mark.parametrize("mode", ["range-mp4"])
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


@pytest.mark.parametrize("mode", ["range-mp4"])
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
    fixture = _start_fixture("range-mp4")
    try:
        upstream = _upstream(fixture)
        server, url = _proxy(upstream, content_type="video/mp4", length=len(MP4), ranged=True)
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


def test_plan_refuses_probe_failure():
    planned = urlproxy.plan("mp4", urlproxy.Probe(False), "hevc")
    assert planned.mode == "fail"
    assert planned.reason == "lan_probe_failed"
    assert planned.ranged is False


def test_plan_unknown_length_is_not_proxyable():
    planned = urlproxy.plan("mp4", urlproxy.Probe(True, "video/mp4", None, True), "hevc")
    assert planned.mode == "cache"
    assert planned.reason == "lan_unknown_length"
    assert planned.ranged is False


def test_plan_no_range_is_not_proxyable():
    planned = urlproxy.plan("mp4", urlproxy.Probe(True, "video/mp4", 100, False), "hevc")
    assert planned.mode == "cache"
    assert planned.reason == "lan_no_range"


def test_plan_hev1_is_rewrap():
    planned = urlproxy.plan(
        "mp4", urlproxy.Probe(True, "video/mp4", 100, True), "hevc", codec_tag="hev1"
    )
    assert planned.mode == "rewrap"
    assert planned.reason == "hev1"


def test_plan_probed_h264_is_not_hev1_rewrap():
    """Claimed x265 + probed avc1 must not take the HEVC rewrap (Deadpool field report)."""
    planned = urlproxy.plan(
        "mp4", urlproxy.Probe(True, "video/mp4", 100, True), "h264", codec_tag="avc1"
    )
    assert planned.mode == "proxy"
    assert planned.reason == "lan"


def test_lan_media_prefers_probed_h264_over_claimed_hevc(monkeypatch):
    from nstream import caster
    from nstream import tracks as tracks_mod

    fixture = _start_fixture("range-mp4")
    try:
        monkeypatch.setattr(urlproxy, "is_remote", lambda url: True)
        monkeypatch.setattr(
            tracks_mod,
            "probe_tracks",
            lambda url, **k: tracks_mod.Tracks(video_codec="h264", codec_tag="avc1"),
        )
        cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
        lan = caster.lan_media(
            cfg, _upstream(fixture), "127.0.0.1", container="mp4", video_codec="hevc"
        )
        assert lan is not None
        try:
            assert lan.plan.mode == "proxy"
            assert lan.plan.reason != "hev1"
        finally:
            if lan.shutdown:
                lan.shutdown()
    finally:
        fixture.shutdown()


def test_lan_media_probed_hev1_rewraps_despite_claimed_h264(monkeypatch):
    from nstream import caster
    from nstream import tracks as tracks_mod

    fixture = _start_fixture("range-mp4")
    try:
        monkeypatch.setattr(urlproxy, "is_remote", lambda url: True)
        monkeypatch.setattr(
            tracks_mod,
            "probe_tracks",
            lambda url, **k: tracks_mod.Tracks(video_codec="hevc", codec_tag="hev1"),
        )
        cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
        lan = caster.lan_media(
            cfg, _upstream(fixture), "127.0.0.1", container="mp4", video_codec="h264"
        )
        assert lan is None  # rewrap is not a LAN proxy
    finally:
        fixture.shutdown()


def test_probe_failure_closed_port():
    probed = urlproxy.probe("http://127.0.0.1:1/missing")
    assert probed.ok is False
    assert urlproxy.plan("mp4", probed, "hevc").mode == "fail"


def test_probe_unknown_length_chunked():
    fixture = _start_fixture("norange-mp4", omit_length=True)
    try:
        probed = urlproxy.probe(_upstream(fixture))
        assert probed.ok
        assert probed.content_length is None
        assert probed.ranged is False
        assert urlproxy.plan("mp4", probed, "hevc").reason == "lan_no_range"
    finally:
        fixture.shutdown()


def test_redirect_https_to_http_is_rejected():
    with pytest.raises(urllib.error.URLError):
        urlproxy._guard_redirect("https://debrid.example/f", "http://cdn.example/f")


def test_redirect_to_private_address_is_rejected():
    fixture = _start_fixture("range-mp4", location="http://127.0.0.1/secret")
    try:
        probed = urlproxy.probe(_upstream(fixture))
        assert probed.ok is False
        with pytest.raises((urllib.error.URLError, OSError)):
            urlproxy.open_upstream(_upstream(fixture), "GET", None)
    finally:
        fixture.shutdown()


def test_lan_media_device_none_fails_closed(monkeypatch):
    from nstream import caster

    monkeypatch.setattr(urlproxy, "is_remote", lambda url: True)
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    assert caster.lan_media(cfg, "https://debrid.example/f.mp4", None, container="mp4") is None


def test_cast_device_none_does_not_wan_direct(monkeypatch):
    from nstream import caster

    monkeypatch.setattr(urlproxy, "is_remote", lambda url: True)
    monkeypatch.setattr(caster, "_cast_senders", lambda *a, **k: pytest.fail("WAN-direct"))
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(cfg, "T", "https://debrid.example/f.mp4", device=None, follow=False)
    assert r.started is False and r.error == "lan_no_bind"


def test_client_disconnect_is_silent(capsys):
    fixture = _start_fixture("range-mp4")
    try:
        server, url = _proxy(
            _upstream(fixture), content_type="video/mp4", length=len(MP4), ranged=True
        )
        try:
            import http.client
            from urllib.parse import urlsplit

            parts = urlsplit(url)
            conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=2)
            conn.request("GET", parts.path, headers={"Range": "bytes=0-"})
            resp = conn.getresponse()
            assert resp.status == 206
            resp.read(8)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
    finally:
        fixture.shutdown()
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_upstream_drop_resumes_including_suffix():
    fixture = _start_fixture("drop-range-mp4", drops_left=1, drop_after=10)
    try:
        upstream = _upstream(fixture)
        server, url = _proxy(upstream, content_type="video/mp4", length=len(MP4), ranged=True)
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=-32"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 206
                assert resp.read() == MP4[-32:]
            # Resume must use the Content-Range start, not 0+sent.
            assert any(
                r.startswith("bytes=") and not r.startswith("bytes=-") for r in fixture.ranges
            )
            starts = []
            for r in fixture.ranges:
                if r.startswith("bytes=") and r[6:7].isdigit():
                    starts.append(int(r[6:].split("-", 1)[0]))
            assert starts
            assert min(starts) == len(MP4) - 32
            assert max(starts) > min(starts)  # a resume from further in
        finally:
            server.shutdown()
            server.server_close()
    finally:
        fixture.shutdown()


def test_no_range_is_not_proxied_as_synthetic_206():
    """A no-Range upstream is not a LAN proxy. plan() says cache; we do not discard-from-0."""
    fixture = _start_fixture("norange-mp4")
    try:
        planned = urlproxy.plan("mp4", urlproxy.probe(_upstream(fixture)), "hevc")
        assert planned.mode == "cache"
        assert planned.reason == "lan_no_range"
        # Defense: a mis-built proxy still must not advertise Accept-Ranges or 206.
        server, url = _proxy(
            _upstream(fixture), content_type="video/mp4",
            length=len(MP4), ranged=False,
        )  # fmt: skip
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=100-199"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers.get("Accept-Ranges") in (None, "", "none")
                assert resp.read() == MP4
        finally:
            server.shutdown()
            server.server_close()
    finally:
        fixture.shutdown()


def test_cast_integration_uses_lan_url_not_secret(monkeypatch):
    """Real caster.cast() against the fixture: catt sees the LAN capability url."""
    from nstream import caster

    fixture = _start_fixture("range-mp4")
    try:
        monkeypatch.setattr(urlproxy, "is_remote", lambda url: True)
        monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
        monkeypatch.setattr(caster, "catt_can_lib_load", lambda: False)
        monkeypatch.setattr(caster, "_poll_wait", lambda *_: None)
        calls: list[list[str]] = []
        infos = iter(
            [
                {"player_state": "PLAYING", "current_time": 1.0, "duration": 10.0},
                {"player_state": "IDLE", "duration": 10.0},
            ]
        )

        class _P:
            def __init__(self, out=""):
                self.returncode = 0
                self.stdout = out
                self.stderr = ""

        def fake_run(cmd, **k):
            calls.append(list(cmd))
            if "info" in cmd:
                return _P(json.dumps(next(infos, {})))
            return _P()

        monkeypatch.setattr(caster.subprocess, "run", fake_run)
        cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
        r = caster.cast(
            cfg, "T", _upstream(fixture),
            device="127.0.0.1", follow=True, container="mp4", video_codec="hevc",
        )  # fmt: skip
        assert r.delivery == "lan" and r.started is True
        launch = next(c for c in calls if "cast" in c)
        joined = " ".join(launch)
        assert SECRET not in joined
        assert "/cast/" in joined
        assert "stream.mp4" in joined
    finally:
        fixture.shutdown()


LAN_POSTER = "http://192.168.1.10:45000/cast/tok/poster.jpg"


def _fake_lan(content_type: str, shutdown=None, poster_url=""):
    from nstream import caster

    name = "stream.webm" if content_type == "video/webm" else "stream.mp4"
    plan = urlproxy.LanPlan("proxy", content_type, 100, True, name, "lan")
    return caster.LanMedia(
        f"http://192.168.1.10:45000/cast/tok/{name}",
        content_type,
        shutdown,
        plan,
        poster_url=poster_url,
    )


def test_cast_feeds_lan_mime_into_catt_lib_play(monkeypatch):
    """LAN proxy mime (video/mp4) reaches ADR 0050 library LOAD: BUFFERED + title + thumb."""
    from nstream import caster

    poster = "https://images.metahub.space/poster/medium/tt7068946/img"
    lan = _fake_lan("video/mp4", poster_url=LAN_POSTER)
    monkeypatch.setattr(caster, "lan_media", lambda *a, **k: lan)
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(caster, "catt_can_lib_load", lambda: True)
    monkeypatch.setattr(caster, "catt_receiver_has_load", lambda *a, **k: False)
    seen: dict = {}

    def fake_play(device, url, **kw):
        seen.update(device=device, url=url, **kw)
        return caster.CATT_LIB_OK

    monkeypatch.setattr(caster, "catt_lib_outcome", fake_play)
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(
        cfg, "The Nice Guys", "https://debrid.example/f.mp4?token=SECRET",
        device="10.0.0.5", follow=False,
        meta=caster.CastMeta(poster=poster),
        container="mp4", video_codec="hevc",
    )  # fmt: skip
    assert r.delivery == "lan" and r.started is True
    assert seen["url"] == lan.url and "SECRET" not in seen["url"]
    assert seen["url"].endswith("/cast/tok/stream.mp4")
    assert seen["content_type"] == "video/mp4"
    assert seen["meta"].content_type == "video/mp4"
    load = caster.catt_play_kwargs(seen["title"], seen["meta"], content_type=seen["content_type"])
    assert load["content_type"] == "video/mp4"
    assert load["stream_type"] == caster.CATT_STREAM_BUFFERED
    assert load["thumb"] == LAN_POSTER
    assert load["title"] == "The Nice Guys"


def test_cast_feeds_webm_lan_mime_into_catt_lib_play(monkeypatch):
    from nstream import caster

    lan = _fake_lan("video/webm")
    monkeypatch.setattr(caster, "lan_media", lambda *a, **k: lan)
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(caster, "catt_can_lib_load", lambda: True)
    monkeypatch.setattr(caster, "catt_receiver_has_load", lambda *a, **k: False)
    seen: dict = {}
    monkeypatch.setattr(
        caster, "catt_lib_outcome",
        lambda device, url, **kw: seen.update(url=url, **kw) or caster.CATT_LIB_OK,
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(
        cfg, "T", "https://debrid.example/f.webm",
        device="10.0.0.5", follow=False, container="webm",
    )  # fmt: skip
    assert r.delivery == "lan" and seen["content_type"] == "video/webm"
    assert seen["url"].endswith("/cast/tok/stream.webm")
    assert seen["meta"].content_type == "video/webm"


def test_cast_skips_lan_shutdown_while_receiver_has_load(monkeypatch):
    """Late library LOAD: content_id is the LAN capability path — do not tear the server down.
    The leftover in-process server is registered so the next reap / cast closes it."""
    from nstream import cast_delivery, caster

    shut = {"n": 0}
    lan = _fake_lan("video/mp4", shutdown=lambda: shut.__setitem__("n", shut["n"] + 1))
    monkeypatch.setattr(caster, "lan_media", lambda *a, **k: lan)
    monkeypatch.setattr(
        caster,
        "_cast_senders",
        lambda *a, **k: cast_delivery.CastResult(
            0.0, 0.0, started=False, error="cast_timeout", delivery="lan"
        ),
    )
    monkeypatch.setattr(caster, "catt_receiver_has_load", lambda dev, url: url == lan.url)
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(cfg, "T", "https://debrid.example/f.mp4", device="10.0.0.5", follow=True)
    assert r.started is False and shut["n"] == 0
    assert serve.reap_proxy_server() is True
    assert shut["n"] == 1
    assert serve.reap_proxy_server() is False


def test_cast_follow_ended_skips_final_catt_info(monkeypatch):
    """Follow already saw IDLE: tear down without another catt info."""
    from nstream import cast_delivery, caster

    shut = {"n": 0}
    lan = _fake_lan("video/mp4", shutdown=lambda: shut.__setitem__("n", shut["n"] + 1))
    monkeypatch.setattr(caster, "lan_media", lambda *a, **k: lan)
    monkeypatch.setattr(
        caster,
        "_cast_senders",
        lambda *a, **k: cast_delivery.CastResult(1.0, 10.0, started=True, delivery="lan"),
    )
    monkeypatch.setattr(
        caster, "catt_receiver_has_load",
        lambda *a, **k: pytest.fail("follow already saw IDLE"),
    )  # fmt: skip
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(cfg, "T", "https://debrid.example/f.mp4", device="10.0.0.5", follow=True)
    assert r.started is True and shut["n"] == 1


POSTER = "https://images.metahub.space/poster/medium/tt7068946/img"


class CastError(Exception):
    """Name-matches catt.error.CastError for `_is_catt_session_wait`."""


_CATT_SESSION_WAIT = "Waiting for the media session to become active timed out after 30 seconds"


def _pychromecast_mc_device(
    seen: dict,
    *,
    raise_after_load: bool = False,
    raise_before_load: BaseException | None = None,
):
    """CattDevice stand-in: play_media_url delegates to a pychromecast-like MC.

    Mirrors catt 0.13.3 `DefaultCastController.play_media_url` →
    `MediaController.play_media` (media_info.metadata first, then title/thumb →
    images[0].url). `controller._controller.play_media` is the sent-signal hook.
    """

    class _MC:
        def play_media(self, url, content_type, **kw):
            metadata: dict[str, object] = {"metadataType": 0}
            info = kw.get("media_info") or {}
            if isinstance(info.get("metadata"), dict):
                metadata.update(info["metadata"])
            if kw.get("title"):
                metadata["title"] = kw["title"]
            if kw.get("thumb"):
                metadata["thumb"] = kw["thumb"]
                metadata["images"] = [{"url": kw["thumb"]}]
            seen["mc"] = {
                "url": url,
                "contentType": content_type,
                "streamType": kw.get("stream_type"),
                "metadata": metadata,
            }

    class _Ctrl:
        def __init__(self):
            self._controller = _MC()

        def prep_app(self):
            return None

        def play_media_url(self, url, **kw):
            if raise_before_load is not None:
                raise raise_before_load
            content_type = kw.get("content_type") or "video/mp4"
            self._controller.play_media(
                url,
                content_type,
                current_time=kw.get("current_time"),
                title=kw.get("title"),
                thumb=kw.get("thumb"),
                subtitles=kw.get("subtitles"),
                stream_type=kw.get("stream_type"),
                media_info=kw.get("media_info"),
            )
            if raise_after_load:
                raise CastError(_CATT_SESSION_WAIT)

    class _Dev:
        def __init__(self, **kw):
            seen["ctor"] = kw
            self._ctrl = _Ctrl()

        @property
        def controller(self):
            return self._ctrl

    return _Dev


def _lan_lib_cast(
    monkeypatch,
    seen,
    *,
    raise_after_load=False,
    raise_before_load=None,
    receiver=None,
    grace=0.0,
    shutdown=None,
):
    from nstream import caster

    def fake_lan(*a, **k):
        src = k.get("poster") or ""
        poster_url = LAN_POSTER if caster.catt_jpeg_poster_url(src) else ""
        return _fake_lan("video/mp4", shutdown=shutdown, poster_url=poster_url)

    monkeypatch.setattr(caster, "lan_media", fake_lan)
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(caster, "catt_can_lib_load", lambda: True)
    monkeypatch.setattr(caster, "catt_inprocess_supports_load_meta", lambda: True)
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: True)
    device_cls = _pychromecast_mc_device(
        seen, raise_after_load=raise_after_load, raise_before_load=raise_before_load
    )
    monkeypatch.setattr(caster, "_catt_device_cls", lambda: device_cls)
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_GRACE", grace)
    monkeypatch.setattr(caster.util, "CATT_LIB_CONFIRM_POLL", 0.01)
    monkeypatch.setattr(caster, "receiver_info", receiver or (lambda dev: {}))
    seen.setdefault("reap", {})
    monkeypatch.setattr(
        caster.serve,
        "schedule_reap",
        lambda fn, seconds, **k: seen["reap"].update(fn=fn, s=seconds, k=k),
    )
    calls: list[list[str]] = []

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(caster.subprocess, "run", lambda cmd, **k: calls.append(list(cmd)) or _P())
    return _fake_lan("video/mp4", shutdown=shutdown, poster_url=LAN_POSTER), calls


def test_lan_cast_lib_load_sends_movie_poster_via_mc(monkeypatch):
    """LAN path: library LOAD is metadataType 1 + images[0] poster (pychromecast MC)."""
    from nstream import caster

    seen: dict = {}
    lan, calls = _lan_lib_cast(monkeypatch, seen)
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(
        cfg, "The Nice Guys", "https://debrid.example/f.mp4?token=SECRET",
        device="10.0.0.5", follow=False,
        meta=caster.CastMeta(poster=POSTER),
        container="mp4", video_codec="hevc",
    )  # fmt: skip
    assert r.delivery == "lan" and r.started is True
    mc = seen["mc"]
    assert mc["url"] == lan.url and "SECRET" not in mc["url"]
    assert mc["contentType"] == "video/mp4"
    assert mc["streamType"] == caster.CATT_STREAM_BUFFERED
    assert mc["metadata"]["metadataType"] == caster.CATT_METADATA_MOVIE
    assert mc["metadata"]["title"] == "The Nice Guys"
    assert mc["metadata"]["images"][0]["url"] == LAN_POSTER
    assert POSTER not in json.dumps(calls)
    assert not any("cast" in c and lan.url in c for c in calls)


def test_lan_cast_lib_session_timeout_no_cli(monkeypatch):
    """Post-LOAD session wait, receiver still empty: keep metadata, honest miss, no CLI."""
    from nstream import caster, notices

    seen: dict = {}
    lan, calls = _lan_lib_cast(monkeypatch, seen, raise_after_load=True)
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    with notices.capture() as bag:
        r = caster.cast(
            cfg, "The Nice Guys", "https://debrid.example/f.mp4",
            device="10.0.0.5", follow=False,
            meta=caster.CastMeta(poster=POSTER),
        )  # fmt: skip
    assert r.delivery == "lan" and r.started is False
    assert r.error == "cast_never_started"
    assert any(n.code == "cast_unconfirmed" for n in bag)
    assert seen["mc"]["metadata"]["metadataType"] == caster.CATT_METADATA_MOVIE
    assert seen["mc"]["metadata"]["images"][0]["url"] == LAN_POSTER
    assert not any("cast" in c and lan.url in c for c in calls)


def test_lan_cast_raise_before_load_falls_to_cli(monkeypatch):
    """play_media_url raises before MediaController.play_media → CLI fallback."""
    from nstream import caster

    seen: dict = {}
    lan, calls = _lan_lib_cast(
        monkeypatch, seen, raise_before_load=TypeError("unexpected keyword argument")
    )
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(
        cfg, "The Nice Guys", "https://debrid.example/f.mp4",
        device="10.0.0.5", follow=False,
        meta=caster.CastMeta(poster=POSTER),
    )  # fmt: skip
    assert r.delivery == "lan" and r.started is True
    assert "mc" not in seen
    launch = next(c for c in calls if "cast" in c and lan.url in c)
    assert "-l" in launch and "--thumb" not in launch


def test_lan_cast_session_wait_later_receiver_match(monkeypatch):
    """Post-LOAD raise; grace poll later sees our media → started, no CLI."""
    from nstream import caster

    seen: dict = {}
    url = "http://192.168.1.10:45000/cast/tok/stream.mp4"
    infos = iter([{}, {"player_state": "BUFFERING", "content_id": url}])
    lan, calls = _lan_lib_cast(
        monkeypatch,
        seen,
        raise_after_load=True,
        receiver=lambda dev: next(infos, {"player_state": "BUFFERING", "content_id": url}),
        grace=0.3,
    )
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(
        cfg, "The Nice Guys", "https://debrid.example/f.mp4",
        device="10.0.0.5", follow=False,
        meta=caster.CastMeta(poster=POSTER),
    )  # fmt: skip
    assert r.delivery == "lan" and r.started is True and r.error is None
    assert seen["mc"]["metadata"]["images"][0]["url"] == LAN_POSTER
    assert not any("cast" in c and lan.url in c for c in calls)


def test_lan_cast_session_wait_load_failed_falls_to_cli(monkeypatch):
    """Post-LOAD raise + receiver LOAD_FAILED → fail, CLI may run."""
    from nstream import caster

    seen: dict = {}
    lan, calls = _lan_lib_cast(
        monkeypatch,
        seen,
        raise_after_load=True,
        receiver=lambda dev: {
            "player_state": "IDLE",
            "idle_reason": "ERROR",
            "error": "LOAD_FAILED",
        },
    )
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(
        cfg, "The Nice Guys", "https://debrid.example/f.mp4",
        device="10.0.0.5", follow=False,
        meta=caster.CastMeta(poster=POSTER),
    )  # fmt: skip
    assert r.delivery == "lan"
    launch = next(c for c in calls if "cast" in c and lan.url in c)
    assert launch and "--thumb" not in launch
    assert r.started is True  # CLI stub returns rc 0


def test_lan_cast_unconfirmed_schedules_reap(monkeypatch):
    """Unconfirmed LAN keep-alive is bounded by CATT_LIB_UNCONFIRMED_SERVE_S."""
    from nstream import caster

    seen: dict = {}
    shut = {"n": 0}
    lan, calls = _lan_lib_cast(
        monkeypatch, seen, raise_after_load=True, shutdown=lambda: shut.__setitem__("n", 1)
    )
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(
        cfg, "The Nice Guys", "https://debrid.example/f.mp4",
        device="10.0.0.5", follow=False,
        meta=caster.CastMeta(poster=POSTER),
    )  # fmt: skip
    assert r.started is False and r.error == "cast_never_started"
    assert not any("cast" in c and lan.url in c for c in calls)
    assert seen["reap"]["s"] == caster.util.CATT_LIB_UNCONFIRMED_SERVE_S
    assert shut["n"] == 0
    seen["reap"]["fn"]()
    assert shut["n"] == 1


def test_lan_cast_cli_when_lib_unavailable(monkeypatch):
    """CLI fallback only when the library loader is unavailable (no --thumb)."""
    from nstream import caster

    lan = _fake_lan("video/mp4")
    monkeypatch.setattr(caster, "lan_media", lambda *a, **k: lan)
    monkeypatch.setattr(caster.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(caster, "catt_can_lib_load", lambda: False)
    monkeypatch.setattr(caster, "catt_supports_load_meta", lambda: True)
    monkeypatch.setattr(caster, "catt_receiver_has_load", lambda *a, **k: False)
    calls: list[list[str]] = []

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(caster.subprocess, "run", lambda cmd, **k: calls.append(list(cmd)) or _P())
    cfg = Config(torrentio_base="tb", cast_lan_proxy=True)
    r = caster.cast(
        cfg, "The Nice Guys", "https://debrid.example/f.mp4?token=SECRET",
        device="10.0.0.5", follow=False,
        meta=caster.CastMeta(poster=POSTER),
    )  # fmt: skip
    assert r.delivery == "lan" and r.started is True
    launch = next(c for c in calls if "cast" in c)
    assert lan.url in launch and "SECRET" not in " ".join(launch)
    assert launch[launch.index("-l") + 1] == "The Nice Guys"
    assert launch[launch.index("--stream-type") + 1] == "BUFFERED"
    assert POSTER not in launch and "--thumb" not in launch
