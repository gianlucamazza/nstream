"""Tier-2 Range HTTP server (`serve.py`): range parsing + a live request/response check that the
DMR's `Range`→`206` exchange (and HEAD) is honoured the way catt's server was, plus the
per-cast secret URL path (token) gate — anything off-path is 404, non-GET/HEAD is 405."""

from __future__ import annotations

import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from nstream import serve


def test_parse_range_basic():
    assert serve._parse_range("bytes=0-99", 1000) == (0, 99)
    assert serve._parse_range("bytes=100-", 1000) == (100, 999)
    assert serve._parse_range("bytes=-50", 1000) == (950, 999)
    assert serve._parse_range("bytes=0-5000", 1000) == (0, 999)  # clamp end


def test_parse_range_rejects():
    assert serve._parse_range("", 1000) is None
    assert serve._parse_range("bytes=2000-", 1000) is None  # start past EOF
    assert serve._parse_range("bytes=0-1,4-5", 1000) is None  # multi-range
    assert serve._parse_range("bytes=abc", 1000) is None


def test_lan_ip_returns_address():
    ip = serve.lan_ip("127.0.0.1")
    assert isinstance(ip, str) and ip.count(".") == 3


def _serve(tmp_path):
    f = tmp_path / "movie.mp4"
    data = bytes(range(256)) * 8  # 2048 bytes of known content
    f.write_bytes(data)
    server, port, _thread = serve.serve_file(str(f), "127.0.0.1")
    return server, port, data


def _url(server, port):
    return serve.served_url("127.0.0.1", port, server.token)


def test_range_request_returns_206(tmp_path):
    server, port, data = _serve(tmp_path)
    try:
        req = urllib.request.Request(_url(server, port), headers={"Range": "bytes=0-9"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 206
            assert resp.headers["Content-Range"] == f"bytes 0-9/{len(data)}"
            assert resp.headers["Content-Length"] == "10"
            assert resp.headers["Accept-Ranges"] == "bytes"
            assert resp.read() == data[:10]
    finally:
        server.shutdown()


def test_full_get_returns_200(tmp_path):
    server, port, data = _serve(tmp_path)
    try:
        with urllib.request.urlopen(_url(server, port), timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Length"] == str(len(data))
            assert resp.read() == data
    finally:
        server.shutdown()


def test_head_has_no_body(tmp_path):
    server, port, data = _serve(tmp_path)
    try:
        req = urllib.request.Request(_url(server, port), method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Length"] == str(len(data))
            assert resp.read() == b""
    finally:
        server.shutdown()


# --- side-loaded WebVTT caption track + CORS --------------------------------


def _serve_with_subs(tmp_path):
    f = tmp_path / "movie.mp4"
    f.write_bytes(b"video-bytes")
    vtt = tmp_path / "eng.vtt"
    vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n")
    server, port, _thread = serve.serve_file(str(f), "127.0.0.1", sub_path=str(vtt))
    return server, port


def test_sub_track_served_as_vtt_with_cors(tmp_path):
    server, port = _serve_with_subs(tmp_path)
    try:
        url = serve.served_sub_url("127.0.0.1", port, server.token)
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/vtt")
            assert resp.headers["Access-Control-Allow-Origin"] == "*"  # Cast requires CORS
            assert resp.read().startswith(b"WEBVTT")
    finally:
        server.shutdown()


def test_media_response_also_carries_cors(tmp_path):
    server, port = _serve_with_subs(tmp_path)
    try:
        with urllib.request.urlopen(_url(server, port), timeout=5) as resp:
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        server.shutdown()


def test_options_preflight_returns_cors(tmp_path):
    server, port = _serve_with_subs(tmp_path)
    try:
        req = urllib.request.Request(_url(server, port), method="OPTIONS")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 204
            assert resp.headers["Access-Control-Allow-Origin"] == "*"
    finally:
        server.shutdown()


def test_sub_path_404_when_no_sub_served(tmp_path):
    # A media-only server must 404 the sub path (no sub_path configured).
    server, port, _data = _serve(tmp_path)
    try:
        url = serve.served_sub_url("127.0.0.1", port, server.token)
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url, timeout=5)
        assert exc.value.code == 404
    finally:
        server.shutdown()


def test_sub_only_server_404s_media_path(tmp_path):
    # Tier-1 direct cast: a sub-only server (no media file) serves the VTT but 404s the media.
    vtt = tmp_path / "eng.vtt"
    vtt.write_text("WEBVTT\n\n")
    server, port, _thread = serve.serve_file(None, "127.0.0.1", sub_path=str(vtt))
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(_url(server, port), timeout=5)
        assert exc.value.code == 404
        with urllib.request.urlopen(
            serve.served_sub_url("127.0.0.1", port, server.token), timeout=5
        ) as resp:
            assert resp.status == 200
    finally:
        server.shutdown()


# --- secret token path (capability URL) -------------------------------------


def test_served_url_carries_token():
    url = serve.served_url("192.168.1.10", 45001, "TOK123")
    assert url == "http://192.168.1.10:45001/cast/TOK123/stream.mp4"


def test_tokens_are_per_cast_and_unguessable():
    a, b = serve.new_token(), serve.new_token()
    assert a != b and len(a) >= 20  # token_urlsafe(16) → 22 chars


def test_wrong_path_is_404(tmp_path):
    """The firewall opens the port to the whole LAN: anything but the exact per-cast
    token path (old fixed path, wrong token, root) must be a flat 404."""
    server, port, _data = _serve(tmp_path)
    try:
        for path in ("/stream.mp4", f"/cast/{'x' * 22}/stream.mp4", "/", "/cast/"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
            assert exc.value.code == 404
    finally:
        server.shutdown()


def test_head_wrong_path_is_404(tmp_path):
    server, port, _data = _serve(tmp_path)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/stream.mp4", method="HEAD")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 404
    finally:
        server.shutdown()


def test_post_is_405_even_on_token_path(tmp_path):
    server, port, _data = _serve(tmp_path)
    try:
        req = urllib.request.Request(_url(server, port), data=b"x", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 405
    finally:
        server.shutdown()


def test_detached_main_announces_port_and_token(tmp_path):
    """The detached entrypoint must announce PORT then TOKEN on stdout (the token is born
    in the server, never on the command line) and serve only on that token path."""
    f = tmp_path / "m.mp4"
    f.write_bytes(b"x" * 16)
    proc = subprocess.Popen(
        [sys.executable, "-m", "nstream.serve", str(f), "--bind", "127.0.0.1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        port_line = proc.stdout.readline().strip()
        token_line = proc.stdout.readline().strip()
        assert port_line.startswith("PORT=") and token_line.startswith("TOKEN=")
        port, token = int(port_line[len("PORT=") :]), token_line[len("TOKEN=") :]
        assert len(token) >= 20
        with urllib.request.urlopen(serve.served_url("127.0.0.1", port, token), timeout=5) as r:
            assert r.status == 200 and r.read() == b"x" * 16
    finally:
        proc.kill()
        proc.wait()


def test_server_header_does_not_leak_versions(tmp_path):
    server, port, _data = _serve(tmp_path)
    try:
        with urllib.request.urlopen(_url(server, port), timeout=5) as resp:
            srv = resp.headers.get("Server", "")
            assert "Python" not in srv and "BaseHTTP" not in srv
    finally:
        server.shutdown()


def test_serve_file_binds_in_cast_range(tmp_path):
    f = tmp_path / "m.mp4"
    f.write_bytes(b"x" * 64)
    server, port, _thread = serve.serve_file(str(f), "127.0.0.1")
    try:
        # Must land in the firewall-allowed cast range so the TV can reach it through UFW.
        assert serve._CAST_PORT_LO <= port <= serve._CAST_PORT_HI
    finally:
        server.shutdown()


# --- ufw auto-ensure -------------------------------------------------------


def test_lan_subnet():
    assert serve._lan_subnet("192.168.1.75") == "192.168.1.0/24"
    assert serve._lan_subnet("10.0.5.42") == "10.0.5.0/24"


def test_ensure_firewall_noop_without_ufw(monkeypatch):
    # No ufw on PATH → silent no-op, never touches subprocess, never raises.
    monkeypatch.setattr(serve.shutil, "which", lambda _: None)
    called = []
    monkeypatch.setattr(serve.subprocess, "run", lambda *a, **k: called.append(a))
    serve.ensure_firewall("192.168.1.75")
    assert called == []


def test_ensure_firewall_adds_rule_when_absent(monkeypatch):
    monkeypatch.setattr(serve.shutil, "which", lambda _: "/usr/bin/ufw")
    cmds = []

    class _R:
        def __init__(self, rc, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **k):
        cmds.append(cmd)
        if cmd[:3] == ["sudo", "-n", "ufw"] and cmd[3] == "status":
            return _R(0, "Status: active\n")  # rule absent
        return _R(0)

    monkeypatch.setattr(serve.subprocess, "run", fake_run)
    serve.ensure_firewall("192.168.1.75")
    allow = next(c for c in cmds if "allow" in c)
    # Byte-identical to catt/skill-cast's rule so ufw dedups to one shared rule.
    assert allow == [
        "sudo",
        "-n",
        "ufw",
        "allow",
        "from",
        "192.168.1.0/24",
        "to",
        "any",
        "port",
        "45000:47000",
        "proto",
        "tcp",
    ]


def test_ensure_firewall_skips_when_present(monkeypatch):
    monkeypatch.setattr(serve.shutil, "which", lambda _: "/usr/bin/ufw")
    cmds = []

    class _R:
        returncode = 0
        stderr = ""
        stdout = "45000:47000/tcp   ALLOW IN   192.168.1.0/24\n"

    def fake_run(cmd, **k):
        cmds.append(cmd)
        return _R()

    monkeypatch.setattr(serve.subprocess, "run", fake_run)
    serve.ensure_firewall("192.168.1.75")
    assert not any("allow" in c for c in cmds)  # already open → no privileged add


def test_ensure_firewall_noop_without_sudo(monkeypatch):
    monkeypatch.setattr(serve.shutil, "which", lambda _: "/usr/bin/ufw")

    class _R:
        returncode = 1  # sudo -n failed (no passwordless) / ufw inactive
        stdout = stderr = ""

    monkeypatch.setattr(serve.subprocess, "run", lambda *a, **k: _R())
    serve.ensure_firewall("192.168.1.75")  # must not raise


def test_firewall_hint_mentions_rule():
    hint = serve.firewall_hint("192.168.1.75", 45123)
    assert "192.168.1.0/24" in hint and "45000:47000" in hint and "45123" in hint


# --- persisted VTT lifecycle (detached sub server, ADR 0018 refinement) -------


def test_persist_sub_copies_into_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    src = tmp_path / "a.vtt"
    src.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nciao\n")
    dst = serve.persist_sub(str(src))
    assert dst is not None and dst != str(src)
    assert Path(dst).read_text() == src.read_text()
    assert Path(dst).parent == tmp_path / "nstream" / "subs"


def test_reap_sub_server_removes_persisted_copy(tmp_path, monkeypatch):
    """The persisted VTT's lifecycle is the server's: reaping the one removes the other
    (otherwise every fire-and-return cast would leak a file in the cache)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(serve, "kill_detached", lambda pid: None)
    persisted = tmp_path / "cast-sub-x.vtt"
    persisted.write_text("WEBVTT\n")
    serve.register_sub_server(12345, str(persisted))
    assert serve.reap_sub_server() is True
    assert not persisted.exists()


def test_reap_sub_server_backcompat_pid_only(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    killed = []
    monkeypatch.setattr(serve, "kill_detached", lambda pid: killed.append(pid))
    serve.register_sub_server(999)
    assert serve.reap_sub_server() is True and killed == [999]
