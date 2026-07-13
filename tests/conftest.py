"""Shared test guards for the nstream suite."""

import pytest

from nstream import bridge


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
