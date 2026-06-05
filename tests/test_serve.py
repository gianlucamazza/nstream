"""Tier-2 Range HTTP server (`serve.py`): range parsing + a live request/response check that the
DMR's `Range`→`206` exchange (and HEAD) is honoured the way catt's server was."""

from __future__ import annotations

import urllib.request

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


def test_range_request_returns_206(tmp_path):
    server, port, data = _serve(tmp_path)
    try:
        req = urllib.request.Request(
            serve.served_url("127.0.0.1", port), headers={"Range": "bytes=0-9"}
        )
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
        with urllib.request.urlopen(serve.served_url("127.0.0.1", port), timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Length"] == str(len(data))
            assert resp.read() == data
    finally:
        server.shutdown()


def test_head_has_no_body(tmp_path):
    server, port, data = _serve(tmp_path)
    try:
        req = urllib.request.Request(serve.served_url("127.0.0.1", port), method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Length"] == str(len(data))
            assert resp.read() == b""
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
