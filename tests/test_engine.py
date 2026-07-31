"""Unit tests for the local P2P playback engine (TorrServer driver).

Network and process I/O are mocked: tests never spawn a real server or touch the network.
The module keeps a process-wide singleton (`_base_url`/`_spawned`); the autouse fixture
resets it so tests don't leak state into one another.
"""

from __future__ import annotations

import socket

import pytest

from nstream import engine
from nstream.config import Config
from nstream.types import Stream


@pytest.fixture(autouse=True)
def _reset_singleton():
    engine._base_url = None
    engine._spawned = None
    yield
    engine._base_url = None
    engine._spawned = None


def _cfg(**kw) -> Config:
    return Config(torrentio_base="tb", **kw)


def test_magnet_includes_hash_name_and_trackers():
    s: Stream = {
        "infoHash": "ABCDEF",
        "title": "Some Movie 2024\n👤 5 💾 2 GB",
        "sources": ["tracker:udp://t1:80", "dht:node", "tracker:http://t2/announce"],
    }
    magnet = engine.magnet_from_stream(s)
    assert magnet.startswith("magnet:?xt=urn:btih:ABCDEF")
    assert "dn=Some%20Movie%202024" in magnet
    assert "tr=udp%3A//t1%3A80" in magnet and "tr=http%3A//t2/announce" in magnet
    assert "dht" not in magnet  # only tracker: sources become tr=


def test_magnet_bare_hash_when_no_extras():
    assert engine.magnet_from_stream({"infoHash": "H"}) == "magnet:?xt=urn:btih:H"


def test_largest_index_picks_biggest_file():
    files = [{"id": 1, "length": 100}, {"id": 2, "length": 900}, {"id": 3, "length": 500}]
    assert engine._largest_index(files) == 2
    assert engine._largest_index([]) == 1  # default when the server reports no file_stats


def test_download_dir_default_and_override(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert engine._download_dir(_cfg()).endswith("/nstream/torrents")
    assert engine._download_dir(_cfg(engine_download_dir="/data/t")) == "/data/t"


def test_installed_reflects_path(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/TorrServer")
    assert engine.installed() is True
    monkeypatch.setattr(engine.shutil, "which", lambda _: None)
    assert engine.installed() is False


def test_binary_accepts_lowercase_name(monkeypatch):
    # The Arch `torrserver-bin` package installs lower-case `torrserver`, not `TorrServer`.
    monkeypatch.setattr(
        engine.shutil, "which", lambda n: "/usr/bin/torrserver" if n == "torrserver" else None
    )
    assert engine._binary() == "/usr/bin/torrserver"
    assert engine.installed() is True


def test_ensure_running_reuses_live_server(monkeypatch):
    # A server already answering /echo on the configured port is reused — never spawned.
    monkeypatch.setattr(engine, "_alive", lambda base: True)
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    assert engine.ensure_running(_cfg(engine_port=8090)) == "http://127.0.0.1:8090"


def test_ensure_running_missing_binary_raises(monkeypatch):
    monkeypatch.setattr(engine, "_alive", lambda base: False)
    monkeypatch.setattr(engine.shutil, "which", lambda _: None)
    with pytest.raises(engine.EngineUnavailable, match="non trovato"):
        engine.ensure_running(_cfg())


def test_ensure_running_spawns_and_waits(monkeypatch, tmp_path):
    # Not alive at first, binary present, then the spawned server starts answering /echo.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    alive = iter([False, False, True])  # ensure() check, spawn-loop miss, then ready
    monkeypatch.setattr(engine, "_alive", lambda base: next(alive))
    monkeypatch.setattr(engine, "_port_taken", lambda port: False)
    monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/TorrServer")

    class _Proc:
        def poll(self):
            return None  # still running

    monkeypatch.setattr(engine.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(engine, "_configure_cache", lambda base, cfg: None)
    monkeypatch.setattr(engine.time, "sleep", lambda _: None)
    monkeypatch.setattr(engine.atexit, "register", lambda fn: None)
    assert engine.ensure_running(_cfg(engine_port=9000)) == "http://127.0.0.1:9000"


def test_ensure_running_raises_if_spawn_exits(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "_alive", lambda base: False)
    monkeypatch.setattr(engine, "_port_taken", lambda port: False)
    monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/TorrServer")

    class _Dead:
        def poll(self):
            return 1  # exited

    monkeypatch.setattr(engine.subprocess, "Popen", lambda *a, **k: _Dead())
    monkeypatch.setattr(engine.time, "sleep", lambda _: None)
    monkeypatch.setattr(engine.atexit, "register", lambda fn: None)
    with pytest.raises(engine.EngineUnavailable, match="uscito"):
        engine.ensure_running(_cfg())


def test_resolve_builds_stream_url_largest_file(monkeypatch):
    monkeypatch.setattr(engine, "ensure_running", lambda cfg: "http://127.0.0.1:8090")
    monkeypatch.setattr(
        engine, "_post", lambda *a, **k: {"hash": "H1", "file_stats": [{"id": 1, "length": 9}]}
    )
    monkeypatch.setattr(engine, "_wait_buffer", lambda base, h: None)
    monkeypatch.setattr(engine, "_lan_ip", lambda: "192.168.1.50")
    url = engine.resolve(_cfg(engine_port=8090), {"infoHash": "H1"})
    assert url == "http://192.168.1.50:8090/stream?link=H1&index=1&play"


def test_resolve_honours_file_idx(monkeypatch):
    monkeypatch.setattr(engine, "ensure_running", lambda cfg: "http://127.0.0.1:8090")
    monkeypatch.setattr(engine, "_post", lambda *a, **k: {"hash": "H2"})
    monkeypatch.setattr(engine, "_wait_buffer", lambda base, h: None)
    monkeypatch.setattr(engine, "_lan_ip", lambda: "10.0.0.2")
    # fileIdx is 0-based in Stremio; TorrServer's index is 1-based → +1.
    url = engine.resolve(_cfg(engine_port=8090), {"infoHash": "H2", "fileIdx": 2})
    assert url.endswith("/stream?link=H2&index=3&play")


def test_resolve_add_failure_raises(monkeypatch):
    monkeypatch.setattr(engine, "ensure_running", lambda cfg: "http://127.0.0.1:8090")

    def _boom(*a, **k):
        raise TimeoutError("no answer")

    monkeypatch.setattr(engine, "_post", _boom)
    with pytest.raises(engine.EngineUnavailable, match="aggiunta torrent fallita"):
        engine.resolve(_cfg(), {"infoHash": "H"})


def test_wait_buffer_returns_when_preloaded(monkeypatch, capsys):
    # Stops as soon as the read-ahead window is full; no sleep loop needed.
    monkeypatch.setattr(
        engine, "_torrent_stat", lambda base, h: {"preload_size": 10, "preloaded_bytes": 10}
    )
    monkeypatch.setattr(engine.time, "sleep", lambda _: pytest.fail("should not loop"))
    engine._wait_buffer("http://127.0.0.1:8090", "H")  # returns promptly
    assert "buffering" in capsys.readouterr().err


def test_wait_buffer_timeout_with_zero_bytes_raises(monkeypatch):
    # Dead torrent (0 peers, 0 bytes ever buffered): the timeout must raise EngineUnavailable
    # so the caller degrades to the next candidate instead of playing an empty buffer.
    monkeypatch.setattr(engine, "_torrent_stat", lambda base, h: {})
    times = iter([0.0, 0.0, 1000.0])  # deadline calc, one loop pass, then expired
    monkeypatch.setattr(engine.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(engine.time, "sleep", lambda _: None)
    with pytest.raises(engine.EngineUnavailable, match="buffer vuoto"):
        engine._wait_buffer("http://127.0.0.1:8090", "H")


def test_wait_buffer_timeout_with_partial_buffer_returns(monkeypatch, capsys):
    # Partial buffer at the deadline: returns anyway (play attempt) with an explicit notice;
    # the progress line shows the % of the read-ahead target while waiting.
    mb = 1024 * 1024
    monkeypatch.setattr(
        engine,
        "_torrent_stat",
        lambda base, h: {"preload_size": 100 * mb, "preloaded_bytes": mb, "active_peers": 1},
    )
    times = iter([0.0, 0.0, 1000.0])
    monkeypatch.setattr(engine.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(engine.time, "sleep", lambda _: None)
    engine._wait_buffer("http://127.0.0.1:8090", "H")  # no exception
    err = capsys.readouterr().err
    assert "buffer parziale" in err
    assert "%" in err  # progress line includes preloaded/preload_size percentage


def test_vpn_active_detects_wireguard(monkeypatch):
    import io

    monkeypatch.setattr(engine.os, "listdir", lambda p: ["eth0", "lo", "wg0"])

    def fake_open(path, *a, **k):
        if path.endswith("wg0/operstate"):
            return io.StringIO("unknown\n")  # wg often reports "unknown" while up
        raise OSError

    monkeypatch.setattr("builtins.open", fake_open)
    assert engine.vpn_active() is True


def test_vpn_active_false_without_vpn_iface(monkeypatch):
    monkeypatch.setattr(engine.os, "listdir", lambda p: ["eth0", "lo", "docker0"])
    assert engine.vpn_active() is False


def test_vpn_active_false_when_iface_down(monkeypatch):
    import io

    monkeypatch.setattr(engine.os, "listdir", lambda p: ["tun0"])
    monkeypatch.setattr("builtins.open", lambda path, *a, **k: io.StringIO("down\n"))
    assert engine.vpn_active() is False


class _FakeSpawned:
    """Stand-in for the spawned TorrServer Popen handle, recording lifecycle calls."""

    pid = 4242

    def __init__(self):
        self.calls = []

    def poll(self):
        return None  # still running

    def terminate(self):
        self.calls.append("terminate")

    def wait(self, timeout=None):
        self.calls.append("wait")


def test_detach_spawned_clears_state_and_disarms_shutdown():
    proc = _FakeSpawned()
    engine._spawned = proc
    engine.detach_spawned()
    assert engine._spawned is None  # handle dropped → the server outlives nstream
    engine._shutdown()  # the atexit hook must now terminate nothing
    assert proc.calls == []


def test_detach_spawned_noop_without_spawn():
    engine._spawned = None
    engine.detach_spawned()  # must not raise
    assert engine._spawned is None


def test_shutdown_without_detach_terminates():
    proc = _FakeSpawned()
    engine._spawned = proc
    engine._shutdown()
    assert "terminate" in proc.calls and engine._spawned is None


def test_wait_buffer_ctrl_c_propagates(monkeypatch):
    """Ctrl-C while buffering must abort the whole flow, not degrade to
    EngineUnavailable (which the multi-candidate loops read as "try the next
    torrent" and keep buffering)."""

    def interrupted(base, h):
        raise KeyboardInterrupt

    monkeypatch.setattr(engine, "_torrent_stat", interrupted)
    with pytest.raises(KeyboardInterrupt):
        engine._wait_buffer("http://127.0.0.1:1", "hash")


# --- startup diagnostics (field case 2026-07-30) ---------------------------


def test_ensure_running_reports_a_busy_port_with_the_fix(monkeypatch):
    """A foreign service holding the port answers no health probe, so the old code spawned
    anyway and reported a bare "exited during startup". Name the cause and the remedy."""
    monkeypatch.setattr(engine, "_alive", lambda base: False)
    monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/TorrServer")
    monkeypatch.setattr(engine, "_port_taken", lambda port: True)
    monkeypatch.setattr(
        engine.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn onto a busy port")
    )
    with pytest.raises(engine.EngineUnavailable, match="engine_port"):
        engine.ensure_running(_cfg(engine_port=8090))


def test_port_taken_detects_a_bound_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert engine._port_taken(port) is True
    assert engine._port_taken(port) is False  # released again


def test_startup_error_prefers_the_error_line(tmp_path):
    log = tmp_path / "torrserver.log"
    log.write_text(
        "2026/07/30 19:30:59 =========== START ===========\n"
        "2026/07/30 19:30:59 Cannot bind HTTP port 8090: listen tcp :8090: address already in use\n"
        "2026/07/30 19:30:59 bye\n"
    )
    assert "address already in use" in engine._startup_error(log)


def test_startup_error_is_empty_without_a_log(tmp_path):
    assert engine._startup_error(tmp_path / "missing.log") == ""


def test_spawn_failure_carries_the_server_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "_alive", lambda base: False)
    monkeypatch.setattr(engine, "_port_taken", lambda port: False)
    monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/TorrServer")

    class _Dead:
        def poll(self):
            engine.server_log_path().write_text("2026/07/30 19:30 Cannot bind HTTP port 8090\n")
            return 1

    monkeypatch.setattr(engine.subprocess, "Popen", lambda *a, **k: _Dead())
    monkeypatch.setattr(engine.time, "sleep", lambda _: None)
    monkeypatch.setattr(engine.atexit, "register", lambda fn: None)
    with pytest.raises(engine.EngineUnavailable, match="Cannot bind HTTP port"):
        engine.ensure_running(_cfg())
