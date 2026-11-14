"""Serialized interactive output; no domain policy or terminal input (ADR 0055).

A context selects the panel receiving updates. Copied contexts let executor jobs
finish into their original panel even after another menu or title takes focus.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import shutil
import sys
import threading
import unicodedata
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, TextIO

from . import log

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def plain(text: str) -> str:
    """One safe display line, with secrets and terminal escape sequences removed."""
    return _CONTROL.sub("", _ANSI.sub("", log.redact(text).replace("\n", " · ").replace("\t", " ")))


def clip(text: str, width: int) -> str:
    """Clip by terminal cells, preserving combining marks and joined emoji clusters."""
    text = plain(text)
    clusters: list[tuple[str, int]] = []
    joined = False
    for char in text:
        if char == "\u200d":
            if clusters:
                value, cells = clusters[-1]
                clusters[-1] = (value + char, cells)
            joined = True
            continue
        cells = (
            0
            if unicodedata.combining(char) or char in {"\ufe0f", "\ufe0e"}
            else (2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
        )
        if clusters and (joined or cells == 0):
            value, previous = clusters[-1]
            clusters[-1] = (value + char, max(previous, cells))
        else:
            clusters.append((char, cells))
        joined = False
    if sum(cells for _, cells in clusters) <= width:
        return text
    result = ""
    used = 0
    for value, cells in clusters:
        if used + cells > max(0, width - 1):
            break
        result += value
        used += cells
    return result + ("…" if width > 0 else "")


@dataclass(frozen=True)
class Update:
    activity: str
    text: str
    state: str = "working"
    completed: float | None = None
    total: float | None = None


@dataclass
class Panel:
    title: str
    destination: str = ""
    footer: str = "Ctrl-C: annulla"
    rows: dict[str, Update] = field(default_factory=dict)
    details: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    notice: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def update(self, event: Update) -> None:
        with self.lock:
            self.rows[event.activity] = event

    def lines(self, width: int, height: int) -> list[str]:
        with self.lock:
            title = self.title + (f" · {self.destination}" if self.destination else "")
            labels = {
                "playback": "Riproduzione",
                "source": "Sorgente",
                "streams": "Fonti",
                "audio": "Audio",
                "subs": "Sottotitoli",
                "prepare": "Preparazione",
                "position": "Posizione",
                "volume": "Volume",
                "activity": "Stato",
            }
            rows = []
            for key in labels:
                if key not in self.rows:
                    continue
                event = self.rows[key]
                playback = self.rows.get("playback")
                if (
                    key in {"streams", "prepare"}
                    and event.state == "done"
                    and playback is not None
                    and playback.state == "done"
                ):
                    continue  # completed startup work belongs in Details once playback starts
                text = event.text
                if event.total and event.total > 0 and event.completed is not None:
                    text += f" · {max(0, min(100, event.completed / event.total * 100)):.0f}%"
                marker = {"working": "…", "done": "✓", "warn": "!", "fail": "×"}.get(
                    event.state, "·"
                )
                rows.append(f"{labels[key]:13} {marker} {text}")
            # Keep playback and actionable warnings visible before secondary details.
            footer: list[str] = []
            for part in self.footer.split(" · "):
                if footer and len(plain(footer[-1] + " · " + part)) < width:
                    footer[-1] += " · " + part
                else:
                    footer.append(part)
            footer = footer[:2]
            room = max(0, height - 3 - len(footer) - bool(self.notice))
            body = rows[:room]
            lines = [title, "─" * width, *body]
            if self.notice:
                lines.append(f"! {self.notice}")
            lines += ["─" * width, *footer]
            if height < 6:
                lines = [title, *(rows[: max(0, height - 2)]), self.footer]
            return [clip(line, width) for line in lines[:height]]


_current: contextvars.ContextVar[Panel | None] = contextvars.ContextVar("tui_panel", default=None)
_lock = threading.RLock()
_visible: Panel | None = None
_suspended = 0
_stream: TextIO | None = None
_last_frame = ""


def current() -> Panel | None:
    return _current.get()


@contextlib.contextmanager
def bind(panel: Panel) -> Iterator[None]:
    """Route worker output without changing terminal focus."""
    token = _current.set(panel)
    try:
        yield
    finally:
        _current.reset(token)


def enabled() -> bool:
    return _stream is not None


def publish(event: Update) -> bool:
    panel = current()
    if not enabled() or panel is None:
        return False
    panel.update(event)
    render()
    return True


def detail(text: str, *, warning: bool = False) -> bool:
    panel = current()
    if not enabled() or panel is None:
        return False
    with panel.lock:
        value = plain(text)
        if not panel.details or panel.details[-1] != value:
            panel.details.append(value)
        if warning:
            panel.notice = value
    render()
    return True


def notice(text: str, level: str) -> bool:
    return detail(text, warning=level in {"warn", "fail"})


def render(*, force: bool = False) -> None:
    global _last_frame
    with _lock:
        if _stream is None or _visible is None or _suspended:
            return
        cols, rows = shutil.get_terminal_size((80, 24))
        width, height = max(1, cols - 1), max(1, min(rows - 1, 13))
        lines = _visible.lines(width, height)
        frame = (
            "\x1b[?25l"
            + "".join(
                f"\x1b[{i + 1};1H\x1b[2K" + (f"\x1b[1m{line}\x1b[0m" if i == 0 else line)
                for i, line in enumerate(lines)
            )
            + "\x1b[J"
        )
        if force or frame != _last_frame:
            _stream.write(frame)
            _stream.flush()
            _last_frame = frame


@contextlib.contextmanager
def focus(panel: Panel) -> Iterator[None]:
    global _visible, _last_frame
    token = _current.set(panel)
    with _lock:
        previous = _visible
        _visible = panel
        _last_frame = ""
    render(force=True)
    try:
        yield
    finally:
        _current.reset(token)
        with _lock:
            _visible = previous
            _last_frame = ""
            if _stream is not None and not _suspended:
                _stream.write("\x1b[?25h\x1b[H\x1b[J")
                _stream.flush()
        render(force=True)


@contextlib.contextmanager
def suspend() -> Iterator[None]:
    """Yield terminal ownership to fzf, input(), or mpv; updates still accumulate."""
    global _suspended, _last_frame
    with _lock:
        _suspended += 1
        if _stream is not None and _suspended == 1:
            _stream.write("\x1b[?25h")
            _stream.flush()
    try:
        yield
    finally:
        with _lock:
            _suspended -= 1
            _last_frame = ""
        render(force=True)


@contextlib.contextmanager
def session() -> Iterator[None]:
    """Enable only for the interactive frontend; JSON output never installs a renderer."""
    global _stream, _visible, _last_frame
    if not (sys.stdin.isatty() and sys.stderr.isatty()) or enabled():
        yield
        return
    from . import notices

    _stream = sys.stderr
    console = _Console(_stream)
    handlers: list[logging.StreamHandler[Any]] = [
        handler
        for handler in logging.getLogger("nstream").handlers
        if isinstance(handler, logging.StreamHandler) and handler.stream is _stream
    ]
    try:
        for handler in handlers:
            handler.setStream(console)
        with contextlib.redirect_stderr(console), notices.presentation(notice):
            yield
    finally:
        for handler in handlers:
            handler.setStream(console.stream)
        with _lock:
            stream, _stream = _stream, None
            _visible = None
            _last_frame = ""
            if stream is not None:
                stream.write("\x1b[?25h\x1b[0m")
                stream.flush()


class _Console:
    """Serialize legacy prints and debug lines; subprocesses still use the real fd."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self.local = threading.local()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, text: str) -> int:
        panel = current() or _visible
        if panel is None:
            with _lock:
                if not _suspended:
                    self.stream.write(text)
            return len(text)
        buf = getattr(self.local, "buf", "") + text
        with bind(panel):
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.strip():
                    detail(line, warning=not line.startswith("["))
        self.local.buf = buf[-8192:]
        return len(text)

    def flush(self) -> None:
        with _lock:
            self.stream.flush()
