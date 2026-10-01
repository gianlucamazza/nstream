"""Unit tests for cast session surface helpers and lifecycle pure functions."""

from __future__ import annotations

from nstream import cast_control, state
from nstream.config import Config
from nstream.state import cast_session as cs


def test_cast_session_label_none_when_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    # Fresh RunState path under tmp
    monkeypatch.setattr(cs.util, "RunState", lambda name: _MemState())
    assert state.cast_session_label() is None
    assert state.cast_session_info() is None


class _MemState:
    _data: dict | None = None

    def read(self):
        return self._data

    def write(self, data):
        type(self)._data = dict(data)

    def clear(self):
        type(self)._data = None


def test_cast_session_label_with_title_and_device(monkeypatch):
    st = _MemState()
    st.write({"title": "Dune", "device": "192.168.1.10", "ts": 9e12, "video_id": "tt1"})
    monkeypatch.setattr(cs.util, "RunState", lambda name: st)
    info = state.cast_session_info()
    assert info is not None and info["title"] == "Dune"
    assert state.cast_session_device() == "192.168.1.10"
    assert "Dune" in (state.cast_session_label() or "")
    assert "192.168.1.10" in (state.cast_session_label() or "")


def test_stop_cast_no_session(monkeypatch):
    monkeypatch.setattr(cast_control.state, "expire_cast_session", lambda: None)
    monkeypatch.setattr(cast_control.state, "cast_session_device", lambda: None)
    monkeypatch.setattr(cast_control.mirror, "stop", lambda: False)
    ok, msg = cast_control.stop_cast(Config())
    assert ok is False
    assert "nessun cast" in msg


def test_stop_cast_calls_backends(monkeypatch):
    monkeypatch.setattr(cast_control.state, "expire_cast_session", lambda: None)
    monkeypatch.setattr(cast_control.state, "cast_session_device", lambda: "10.0.0.1")
    monkeypatch.setattr(cast_control.mirror, "stop", lambda: False)
    monkeypatch.setattr(
        cast_control.caster, "status", lambda d: {"position": 10.0, "duration": 100.0, "title": "X"}
    )
    monkeypatch.setattr(cast_control.caster, "stop", lambda d: True)
    monkeypatch.setattr(cast_control.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(cast_control.remux, "stop", lambda d: False)
    seen = {}

    def upd(cfg, device, pos, dur, **kw):
        seen.update(device=device, pos=pos, clear=kw.get("clear"))

    monkeypatch.setattr(cast_control.state, "update_from_receiver", upd)
    ok, msg = cast_control.stop_cast(Config())
    assert ok is True
    assert "10.0.0.1" in msg
    assert seen["clear"] is True and seen["pos"] == 10.0


def test_set_cast_volume(monkeypatch):
    monkeypatch.setattr(cast_control.state, "cast_session_device", lambda: "tv")
    monkeypatch.setattr(cast_control.caster, "set_volume", lambda d, n: n == 50)
    ok, msg = cast_control.set_cast_volume(50)
    assert ok and "50%" in msg


def test_runtime_health_shape():
    rows = cast_control.runtime_health()
    names = [n for n, _, _ in rows]
    assert "mpv" in names
    assert "fzf" in names
    assert "TorrServer" in names
    assert "catt" in names
    assert "castbridge" in names
    assert "mirror sender" in names
    assert "ffprobe" in names
    assert cast_control.health_summary()


def test_cast_status_no_session(monkeypatch):
    monkeypatch.setattr(cast_control.state, "expire_cast_session", lambda: None)
    monkeypatch.setattr(cast_control.state, "cast_session_device", lambda: None)
    ok, msg = cast_control.cast_status()
    assert ok is False
    assert "nessun cast" in msg


def test_cast_status_with_device(monkeypatch):
    monkeypatch.setattr(cast_control.state, "expire_cast_session", lambda: None)
    monkeypatch.setattr(cast_control.state, "cast_session_device", lambda: "10.0.0.2")
    monkeypatch.setattr(
        cast_control.caster,
        "status",
        lambda d: {
            "title": "Film",
            "player_state": "PLAYING",
            "position": 42.0,
            "duration": 100.0,
            "volume": 0.5,
        },
    )
    ok, msg = cast_control.cast_status()
    assert ok is True
    assert "Film" in msg and "42" in msg and "vol 50%" in msg


def test_media_control_pause_via_bridge(monkeypatch):
    monkeypatch.setattr(cast_control.state, "cast_session_device", lambda: "tv")
    monkeypatch.setattr(cast_control.bridge, "bridge_available", lambda: True)
    monkeypatch.setattr(cast_control.bridge, "control", lambda d, c, v: c == "pause")
    ok, msg = cast_control.media_control("pause")
    assert ok and "pausa" in msg


def test_media_control_seek_catt_fallback(monkeypatch):
    monkeypatch.setattr(cast_control.state, "cast_session_device", lambda: "tv")
    monkeypatch.setattr(cast_control.bridge, "bridge_available", lambda: False)
    seen = {}

    class R:
        returncode = 0

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return R()

    monkeypatch.setattr(cast_control.util, "run_cmd", fake_run)
    ok, msg = cast_control.media_control("seek", value=90.0)
    assert ok and "90" in msg
    assert seen["argv"][:4] == ["catt", "-d", "tv", "seek"]


def test_stop_cast_no_session_still_reclaims_local_server(monkeypatch):
    # The session expired (or the TV is off) but a detached remux server still pins a
    # multi-GB temp file: stop must reclaim it rather than answer "nessun cast attivo".
    calls: list = []
    monkeypatch.setattr(cast_control.state, "expire_cast_session", lambda: None)
    monkeypatch.setattr(cast_control.state, "cast_session_device", lambda: None)
    monkeypatch.setattr(cast_control.mirror, "stop", lambda: False)
    monkeypatch.setattr(cast_control.remux, "stop", lambda dev: calls.append(dev) or True)
    ok, msg = cast_control.stop_cast(Config())
    assert ok is True and calls == [None]
    assert "server locale" in msg
