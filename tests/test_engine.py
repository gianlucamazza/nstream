"""Unit tests for the local P2P playback engine (TorrServer driver).

Network and process I/O are mocked: tests never spawn a real server or touch the network.
The module keeps a process-wide singleton (`_base_url`/`_spawned`); the autouse fixture
resets it so tests don't leak state into one another.
"""

from __future__ import annotations

import pytest

from nstream import engine
from nstream.config import Config, Stream


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
    magnet = engine._magnet(s)
    assert magnet.startswith("magnet:?xt=urn:btih:ABCDEF")
    assert "dn=Some%20Movie%202024" in magnet
    assert "tr=udp%3A//t1%3A80" in magnet and "tr=http%3A//t2/announce" in magnet
    assert "dht" not in magnet  # only tracker: sources become tr=


def test_magnet_bare_hash_when_no_extras():
    assert engine._magnet({"infoHash": "H"}) == "magnet:?xt=urn:btih:H"


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


def test_ensure_running_spawns_and_waits(monkeypatch):
    # Not alive at first, binary present, then the spawned server starts answering /echo.
    alive = iter([False, False, True])  # ensure() check, spawn-loop miss, then ready
    monkeypatch.setattr(engine, "_alive", lambda base: next(alive))
    monkeypatch.setattr(engine.shutil, "which", lambda _: "/usr/bin/TorrServer")

    class _Proc:
        def poll(self):
            return None  # still running

    monkeypatch.setattr(engine.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(engine, "_configure_cache", lambda base, cfg: None)
    monkeypatch.setattr(engine.time, "sleep", lambda _: None)
    monkeypatch.setattr(engine.atexit, "register", lambda fn: None)
    assert engine.ensure_running(_cfg(engine_port=9000)) == "http://127.0.0.1:9000"


def test_ensure_running_raises_if_spawn_exits(monkeypatch):
    monkeypatch.setattr(engine, "_alive", lambda base: False)
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
