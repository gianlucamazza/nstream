"""Shared test guards for the nstream suite."""

import os
import socket
from pathlib import Path

import pytest

from nstream import api, bridge, quality

# Keep subprocess smoke tests on the checkout too. Some developer machines have an
# older globally installed nstream, which otherwise makes `python -m nstream` test the
# wrong code even though pytest itself uses pyproject's pythonpath setting.
_SRC = str(Path(__file__).parents[1] / "src")
os.environ["PYTHONPATH"] = _SRC + os.pathsep + os.environ.get("PYTHONPATH", "")


@pytest.fixture(autouse=True)
def _no_host_gpu_probe(monkeypatch, request):
    if request.path.name != "test_quality.py":
        monkeypatch.setattr(quality, "detect_caps", lambda: quality.HwCaps())


@pytest.fixture(autouse=True)
def _isolated_storage(monkeypatch, tmp_path):
    for variable, directory in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        root = tmp_path / directory
        root.mkdir(exist_ok=True)
        monkeypatch.setenv(variable, str(root))
    config = tmp_path / "config" / "nstream"
    config.mkdir()
    (config / "config.json").write_text("{}")


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    connect = socket.socket.connect

    def local_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise AssertionError("test attempted external network access")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)


@pytest.fixture(autouse=True)
def _isolated_runtime_dir(monkeypatch, tmp_path):
    """Point RunState files (cast session, remux/mirror slots) at a per-test dir.

    `cast_flow.run_cast` clears the cast session as a side effect: without this,
    running the suite on a dev machine would delete the REAL
    `$XDG_RUNTIME_DIR/nstream-watch.json` of a cast in progress. Tests that care
    about the exact path still set the env themselves.
    """
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))


@pytest.fixture(autouse=True)
def _no_real_castbridge(monkeypatch):
    """Unit tests must never reach (or spawn) the real castbridge daemon.

    On a dev machine the binary exists, so an unmocked `bridge_available()` is True
    and any cast/status code path would `ensure_daemon()` a REAL `castbridge
    --daemon` that outlives the suite (field-found 2026-06-06: leaked into the
    jarvis-orchestrate service cgroup via the failing-test detection). On machines
    without the binary the same tests silently took the catt path — environment-
    dependent behavior either way. Default both gates to off; tests that exercise
    the bridge path re-patch them explicitly (and mock `cast_load`).
    """
    monkeypatch.setattr(bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(bridge, "ensure_daemon", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _no_real_runtime_lookup(monkeypatch, request):
    """The duration vetting (ADR 0028) asks Cinemeta for the title's runtime on every play
    path. Unmocked, that is a REAL HTTP request from a unit test — slow, flaky offline, and
    dependent on someone else's data. Default it to "unknown" (which turns the guard off,
    exactly as in production when the meta has no runtime); tests that exercise the guard
    set a value themselves. `test_api.py` owns the function under test, so it opts out."""
    if request.path.name == "test_api.py":
        return
    monkeypatch.setattr(api, "expected_runtime_s", lambda cfg, typ, video_id: 0.0)


@pytest.fixture(autouse=True)
def _no_real_episode_lookup(monkeypatch, request):
    """Same guard for the continuation policy (ADR 0029): `series.next_up` fetches the
    episode list to find the next one, and an unmocked call is a real Cinemeta request.
    Default it to empty — which the policy treats as "resume", never as a reason to block —
    so a test that forgets to stub it fails loudly on its assertion, not silently over the
    network. `test_api.py` owns `episodes` itself."""
    if request.path.name == "test_api.py":
        return
    monkeypatch.setattr(api, "episodes", lambda cfg, series_id: [])
