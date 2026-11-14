"""Cast monitor lifetime, main-thread prompts and controls without a receiver."""

from __future__ import annotations

import io
import os
import pty
import termios
import threading
import time
from pathlib import Path

import pytest

from nstream import cast_flow, tui_cast, tui_output, util
from nstream.config import Config, PlayOpts


def outcome(*, advance=False):
    return cast_flow.CastOutcome(
        pos=62,
        dur=300,
        advance=advance,
        action="cast",
        stream={},
        reencoded=False,
        notice=None,
        audio_lang="ita",
        audio_verified=True,
        safety_sub_lang=None,
        sub_paths=(),
        sub_match=None,
        sub_offset=None,
        subs_delivered=False,
        started=True,
        cast_error=None,
    )


@pytest.fixture
def task(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(tui_output, "_stream", io.StringIO())
    monkeypatch.setattr(tui_output, "render", lambda **kwargs: None)
    monkeypatch.setattr(tui_cast, "_active", None)
    opts = PlayOpts(
        auto=True,
        cast=True,
        sub_mode=None,
        sub_lang=None,
        history=True,
        autoplay=True,
        quality=1080,
        interactive=True,
    )
    task = tui_cast.CastTask(
        Config(),
        [{}],
        {},
        device="192.0.2.1",
        title="Film",
        opts=opts,
        panel=tui_output.Panel("Film"),
        typ="movie",
        video_id="tt1",
    )
    monkeypatch.setattr(task, "refresh", lambda: None)
    yield task
    task.cancelled.set()
    if task.thread.is_alive():
        task.thread.join(timeout=1)
    assert not task.thread.is_alive()


def wait_command(task):
    assert task.command_lock.acquire(timeout=1), "command did not finish"
    task.command_lock.release()


def test_return_home_keeps_backend_and_working_files_then_reopens(task, monkeypatch):
    finish = threading.Event()
    seen = {}
    saves = []
    task.on_save = lambda pos, dur: saves.append((pos, dur))

    def run(*args, **kwargs):
        seen["work"] = Path(kwargs["work_dir"])
        marker = seen["work"] / "subtitle.srt"
        marker.write_text("subtitle")
        kwargs["on_event"]({"kind": "started"})
        kwargs["on_event"]({"kind": "playing", "position": 62, "duration": 300})
        assert finish.wait(2)
        assert marker.exists()
        return outcome()

    monkeypatch.setattr(tui_cast.cast_flow, "run_cast", run)
    monkeypatch.setattr(
        tui_cast.cast_control, "stop_cast", lambda *a, **k: pytest.fail("Esc stopped cast")
    )
    monkeypatch.setattr(tui_cast, "read_key", lambda: "\x1b")
    task.thread.start()
    try:
        assert task.started.wait(1)
        assert task.open() is True
        assert task.thread.is_alive() and seen["work"].exists()
        monkeypatch.setattr(tui_cast, "_active", task)
        assert tui_cast.reopen() is True
        assert task.thread.is_alive()
    finally:
        finish.set()
        task.thread.join(timeout=1)
    assert task.done.is_set() and not seen["work"].exists()
    assert saves[-1] == (62, 300)


def test_startup_cancel_unwinds_without_stopping_previous_receiver(task, monkeypatch):
    entered = threading.Event()
    work = []

    def run(*args, **kwargs):
        work.append(Path(kwargs["work_dir"]))
        entered.set()
        while True:
            util.check_cancelled()
            time.sleep(0.001)

    monkeypatch.setattr(tui_cast.cast_flow, "run_cast", run)
    monkeypatch.setattr(
        tui_cast.cast_control, "stop_cast", lambda *a, **k: pytest.fail("stopped previous cast")
    )
    monkeypatch.setattr(tui_cast, "read_key", lambda: "\x1b")
    task.thread.start()
    assert entered.wait(1)
    assert task.open() is False
    task.thread.join(timeout=1)
    assert task.error is None and task.outcome is None
    assert not work[0].exists()


def test_pause_seek_volume_and_failure_feedback(task, monkeypatch):
    task.started.set()
    status = {"player_state": "PAUSED", "position": 10, "duration": 100, "volume_percent": 98}
    monkeypatch.setattr(tui_cast.caster, "status", lambda device: status)
    actions = []

    def control(command, **kwargs):
        actions.append((command, kwargs))
        return False, "Ricevitore non raggiungibile"

    monkeypatch.setattr(tui_cast.cast_control, "media_control", control)
    monkeypatch.setattr(
        tui_cast.cast_control,
        "set_cast_volume",
        lambda level, **kw: (actions.append(("volume", level)), (True, "Volume aggiornato"))[1],
    )
    task.control(" ")
    wait_command(task)
    assert actions[-1][0] == "play"
    assert task.panel.rows["activity"].state == "fail"
    task.control("\x1b[D")
    wait_command(task)
    assert actions[-1][1]["value"] == 0
    task.control("+")
    wait_command(task)
    assert actions[-1] == ("volume", 100)
    status.pop("volume_percent")
    task.control("-")
    wait_command(task)
    assert task.panel.notice == "Volume non disponibile"


def test_worker_prompts_run_on_main_thread(task, monkeypatch):
    threads = []
    called = threading.Event()

    def choose(*args, **kwargs):
        threads.append(threading.current_thread())
        return "ita"

    def run(*args, **kwargs):
        called.set()
        assert kwargs["opts"].choose([], "audio> ") == "ita"
        return outcome()

    task.opts = tui_cast.replace(task.opts, choose=choose)
    task.attached.set()
    monkeypatch.setattr(tui_cast.cast_flow, "run_cast", run)
    task.thread.start()
    assert called.wait(1)
    deadline = time.monotonic() + 1
    while not task.done.is_set() and time.monotonic() < deadline:
        task.service_prompts()
        task.done.wait(0.001)
    assert task.done.is_set() and task.error is None
    assert threads == [threading.main_thread()]


def test_explicit_stop_never_advances_episode(task, monkeypatch):
    finish = threading.Event()

    def run(*args, **kwargs):
        kwargs["on_event"]({"kind": "started"})
        assert finish.wait(2)
        return outcome(advance=True)

    monkeypatch.setattr(tui_cast.cast_flow, "run_cast", run)
    monkeypatch.setattr(
        tui_cast.cast_control, "stop_cast", lambda *a, **kw: (finish.set(), (True, "Fermato"))[1]
    )
    task.thread.start()
    try:
        assert task.started.wait(1)
        task.control("s")
        wait_command(task)
        task.thread.join(timeout=1)
        assert task.outcome is not None and not task.outcome.advance
    finally:
        finish.set()


def test_audio_restarts_shared_vetting_at_current_position(task, monkeypatch):
    first_done, second_started, finish = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        kwargs["on_event"]({"kind": "started"})
        if len(calls) == 1:
            assert first_done.wait(2)
        else:
            second_started.set()
            assert finish.wait(2)
        return outcome()

    monkeypatch.setattr(tui_cast.cast_flow, "run_cast", run)
    monkeypatch.setattr(tui_cast.cast_vet, "cast_languages", lambda *a, **kw: ("ita", "eng"))
    monkeypatch.setattr(tui_cast, "fzf", lambda *a, **kw: "eng")
    monkeypatch.setattr(tui_cast.caster, "status", lambda device: {"position": 62})
    monkeypatch.setattr(
        tui_cast.cast_control,
        "stop_cast",
        lambda *a, **kw: (first_done.set(), (True, "Fermato"))[1],
    )
    task.attached.set()
    task.thread.start()
    try:
        assert task.started.wait(1)
        task.audio()
        deadline = time.monotonic() + 1
        while not second_started.is_set() and time.monotonic() < deadline:
            task.service_prompts()
            second_started.wait(0.001)
        assert second_started.is_set()
        assert calls[1]["opts"].audio_lang == "eng"
        assert calls[1]["opts"].quality == 1080
        assert calls[1]["start"] == 62
        assert not calls[1]["allow_lang_switch"]  # backend must not compete for stdin
    finally:
        first_done.set()
        finish.set()
        task.thread.join(timeout=1)


@pytest.mark.parametrize("key", [b"\x1b", b"\x1b[D", b"\x1b[C", b"\x1bOD", b" ", b"+"])
def test_read_key_consumes_arrow_sequence_and_restores_terminal(monkeypatch, key):
    master, slave = pty.openpty()
    stream = os.fdopen(os.dup(slave), "r")
    monkeypatch.setattr(tui_cast.sys, "stdin", stream)
    saved = termios.tcgetattr(slave)
    ready = threading.Event()
    original = tui_cast.tty.setcbreak

    def cbreak(fd, **kwargs):
        original(fd, **kwargs)
        ready.set()

    monkeypatch.setattr(tui_cast.tty, "setcbreak", cbreak)

    def write():
        assert ready.wait(1)
        os.write(master, key)

    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert tui_cast.read_key(timeout=1) == key.decode()
        assert termios.tcgetattr(slave) == saved
    finally:
        writer.join(timeout=1)
        stream.close()
        os.close(master)
        os.close(slave)


def test_stop_intent_suppresses_advance_before_receiver_reply(task, monkeypatch):
    finish, reply = threading.Event(), threading.Event()

    def run(*args, **kwargs):
        kwargs["on_event"]({"kind": "started"})
        assert finish.wait(2)
        return outcome(advance=True)

    def stop(*args, **kwargs):
        finish.set()
        assert reply.wait(2)
        return True, "Fermato"

    monkeypatch.setattr(tui_cast.cast_flow, "run_cast", run)
    monkeypatch.setattr(tui_cast.cast_control, "stop_cast", stop)
    task.thread.start()
    try:
        assert task.started.wait(1)
        task.control("s")
        assert task.done.wait(1)
        assert task.outcome is not None and not task.outcome.advance
    finally:
        reply.set()
        finish.set()
        wait_command(task)
        task.thread.join(timeout=1)


def test_background_failure_is_reported_once(task, monkeypatch):
    task.error = RuntimeError("Sorgente non disponibile")
    task.done.set()
    monkeypatch.setattr(tui_cast, "_active", task)
    assert tui_cast.finished_notice() == "Sorgente non disponibile"
    assert tui_cast.finished_notice() is None


def test_existing_cast_reopens_without_load_or_source_list(task, monkeypatch):
    monkeypatch.setattr(tui_cast.cast_flow, "run_cast", lambda *a, **kw: pytest.fail("issued LOAD"))
    monkeypatch.setattr(
        tui_cast.caster,
        "status",
        lambda device: {
            "player_state": "PLAYING",
            "position": 62,
            "duration": 300,
        },
    )

    def escape():
        existing = tui_cast.active()
        assert existing is not None and existing.started.wait(1)
        return "\x1b"

    monkeypatch.setattr(tui_cast, "read_key", escape)
    existing = None
    try:
        tui_cast.watch_existing(Config(), {"title": "Film", "device": "192.0.2.1"})
        existing = tui_cast.active()
        assert existing is not None and existing.thread.is_alive()
        assert "a: audio" not in existing.help()
        assert tui_cast.reopen()
    finally:
        if existing is not None:
            existing.cancelled.set()
            existing.thread.join(timeout=1)
            assert existing.done.is_set()


def test_computer_key_uses_core_transfer_after_startup(task, monkeypatch):
    calls = []
    monkeypatch.setattr(
        tui_cast.handoff,
        "transfer_to_local",
        lambda cfg, **kw: calls.append(kw) or (True, "Transferred"),
    )
    task.started.set()
    task.control("c")
    wait_command(task)
    assert calls == [{"device": task.device}]
