"""Terminal ownership, concurrent updates and safe adaptive panel rendering."""

from __future__ import annotations

import io
import logging
import threading
from contextvars import copy_context

import pytest

from nstream import notices, tui_output, ui


class Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def terminal(monkeypatch):
    stream = Tty()
    monkeypatch.setattr(tui_output.sys, "stderr", stream)
    monkeypatch.setattr(tui_output.sys, "stdin", Tty())
    monkeypatch.setattr(tui_output.shutil, "get_terminal_size", lambda fallback: (80, 24))
    with tui_output.session():
        yield stream
    assert tui_output._stream is None
    assert tui_output._visible is None
    assert tui_output._suspended == 0
    assert "\x1b[?25h" in stream.getvalue()


def test_concurrent_activities_wait_for_menu_and_remain_separate(terminal):
    panel = tui_output.Panel("I See Ice")
    with tui_output.focus(panel), tui_output.suspend():
        before = terminal.getvalue()
        threads = []
        for activity in ("streams", "prepare", "subs"):
            context = copy_context()
            thread = threading.Thread(
                target=context.run,
                args=(
                    tui_output.publish,
                    tui_output.Update(activity, activity, "done"),
                ),
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive()
        notices.emit("audio ita non disponibile", code="audio_lang_absent")
        assert terminal.getvalue() == before
    assert set(panel.rows) == {"streams", "prepare", "subs"}
    assert panel.notice == "audio ita non disponibile"
    output = terminal.getvalue()
    assert "Fonti" in output and "Preparazione" in output and "Sottotitoli" in output


def test_late_job_updates_its_original_panel(terminal):
    original, newer = tui_output.Panel("Originale"), tui_output.Panel("Nuovo")
    with tui_output.focus(original):
        context = copy_context()
    with tui_output.focus(newer):
        before = terminal.getvalue()
        context.run(tui_output.publish, tui_output.Update("subs", "Non trovati", "warn"))
        assert terminal.getvalue() == before
        assert not newer.rows
    assert original.rows["subs"].text == "Non trovati"


def test_notices_keep_machine_capture_and_debug_lines_share_writer(terminal):
    panel = tui_output.Panel("Film")
    logger = logging.getLogger("nstream")
    console = tui_output._Console(terminal)
    handler = logging.StreamHandler(console)
    # This handler is added after session entry, like legacy prints; the proxy still owns it.
    logger.addHandler(handler)
    try:
        with tui_output.focus(panel), notices.capture() as bag:
            notices.emit("audio non disponibile", code="audio_lang_absent")
            logger.warning("upstream https://example.test/private?token=secret")
            print("messaggio precedente", file=console)
        assert bag[0].code == "audio_lang_absent"
        details = " ".join(panel.details)
        assert "messaggio precedente" in details
        assert "<url>" in details
        assert "secret" not in terminal.getvalue()
    finally:
        logger.removeHandler(handler)


def test_restore_cursor_on_interrupt(terminal):
    with pytest.raises(KeyboardInterrupt), tui_output.focus(tui_output.Panel("Film")):
        raise KeyboardInterrupt
    assert tui_output.current() is None
    assert "\x1b[?25h" in terminal.getvalue()


@pytest.mark.parametrize("width,height", [(117, 24), (79, 24), (49, 16), (29, 8), (12, 3)])
def test_panel_clips_long_unicode_and_preserves_exit_hint(width, height):
    panel = tui_output.Panel("長い映画 👩‍💻 " * 20, "192.168.1.228")
    panel.footer = "Esc: home · s: ferma · Spazio: pausa · ←/→: ±30s · +/-: volume · d: dettagli"
    for activity in ("playback", "audio", "subs", "prepare"):
        panel.update(tui_output.Update(activity, "日本語 con testo molto lungo " * 10))
    panel.notice = "Italiano non disponibile"
    lines = panel.lines(width, height)
    assert len(lines) <= height
    assert all(tui_output.clip(line, width) == line for line in lines)
    assert any("Esc: home" in line for line in lines)
    assert not any("%" in line for line in lines)


def test_clip_strips_escapes_controls_and_secrets():
    assert tui_output.clip("é日本語", 5) == "é日…"
    assert tui_output.clip("e\u0301日本", 4) == "e\u0301日…"
    assert tui_output.clip("👩‍💻x", 3) == "👩‍💻x"
    assert tui_output.plain("\x1b[31mfoo\x1b[0m\x1b]52;c;bad\x07\x00") == "foo"
    assert tui_output.plain("https://host.test/arbitrary-secret") == "<url>"


def test_redirected_output_is_linear_and_headless_metadata_silent(monkeypatch, capsys):
    monkeypatch.setattr(tui_output.sys.stdin, "isatty", lambda: False)
    with tui_output.session():
        ui.activity("audio", "Italiano", state="done")
        ui.progress("fonti 1/2", activity="streams")
        ui.progress_done("fonti 2/2", activity="streams")
    assert capsys.readouterr().err == "fonti 1/2\nfonti 2/2\n"


def test_session_routes_preexisting_debug_handler_and_restores_it(monkeypatch):
    stream = Tty()
    monkeypatch.setattr(tui_output.sys, "stderr", stream)
    monkeypatch.setattr(tui_output.sys, "stdin", Tty())
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("nstream")
    logger.addHandler(handler)
    panel = tui_output.Panel("Film")
    try:
        with tui_output.session(), tui_output.focus(panel):
            logger.warning("debug: https://example.test/secret")
        assert handler.stream is stream
        assert "<url>" in " ".join(panel.details)
        assert "secret" not in stream.getvalue()
    finally:
        logger.removeHandler(handler)


def test_unscoped_library_output_uses_visible_panel(terminal):
    from contextvars import Context

    panel = tui_output.Panel("Film")
    console = tui_output._Console(terminal)
    with tui_output.focus(panel):
        # A third-party thread has no copied task context, but must not print over the panel.
        Context().run(console.write, "Avviso della libreria\n")
    assert "Avviso della libreria" in panel.details
