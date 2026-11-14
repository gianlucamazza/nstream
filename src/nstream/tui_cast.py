"""Frontend cast panel and keyboard ownership, independent of backend monitoring."""

from __future__ import annotations

import contextlib
import os
import queue
import select
import sys
import tempfile
import termios
import threading
import time
import tty
from collections.abc import Callable
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from typing import Any

from . import (
    cast_control,
    cast_flow,
    cast_vet,
    caster,
    failures,
    handoff,
    languages,
    log,
    tui_output,
    util,
)
from .config import Config, PlayOpts
from .picker import fzf, fzf_index
from .types import Stream

_log = log.get_logger("tui_cast")


class CastBusy(RuntimeError):
    """A previous monitor must finish cleanup before another cast can own its services."""


class HomeRequested(Exception):
    """Leave nested title/episode menus without ending the monitored cast."""


@dataclass
class Prompt:
    callback: Callable
    args: tuple
    kwargs: dict
    ready: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None


def read_key(timeout: float = 0.1) -> str | None:
    """Read one complete key in cbreak mode, restoring the terminal before any menu."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd, when=termios.TCSANOW)
        if not select.select([fd], [], [], timeout)[0]:
            return None
        data = os.read(fd, 1)
        if not data:
            raise EOFError
        key = data.decode("ascii", errors="ignore")
        if key == "\x1b":
            # Distinguish Esc from a complete cursor-key sequence; never leak '[D'.
            for _ in range(8):
                if not select.select([fd], [], [], 0.03)[0]:
                    break
                key += os.read(fd, 1).decode("ascii", errors="ignore")
                if len(key) > 2 and (key[-1].isalpha() or key.endswith("~")):
                    break
        return key
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def clock(seconds: float) -> str:
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


class CastTask:
    """One followed cast owns its working files until the backend finishes.

    Only the main thread opens menus or reads keys. The worker can request an injected
    prompt, and a separate bounded command worker keeps receiver calls off the UI loop.
    """

    def __init__(
        self,
        cfg: Config,
        results: list[Stream],
        chosen: Stream,
        *,
        device: str,
        title: str,
        opts: PlayOpts,
        panel: tui_output.Panel,
        on_save: Callable | None = None,
        remote: bool = False,
        **kwargs,
    ):
        self.cfg, self.results, self.chosen = cfg, results, chosen
        self.device, self.title, self.opts, self.panel = device, title, opts, panel
        self.kwargs, self.on_save = kwargs, on_save
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.stop_requested = threading.Event()
        self.started = threading.Event()
        self.attached = threading.Event()
        self.prompts: queue.Queue[Prompt] = queue.Queue()
        self.outcome: cast_flow.CastOutcome | None = None
        self.error: BaseException | None = None
        self.snapshot: dict = {}
        self.lock = threading.RLock()
        self.command_lock = threading.Lock()
        self.poll_lock = threading.Lock()
        self.remote = remote
        self.next_audio: str | None = None
        self.next_start: float | None = None
        self.last_save = 0.0
        self.last_poll = 0.0
        self.mirror = False
        self.stopping = False
        self.reported = False
        self.thread = threading.Thread(
            target=copy_context().run,
            args=(self._watch_remote if remote else self._work,),
            daemon=True,
            name="tui-cast",
        )

    def _watch_remote(self) -> None:
        with tui_output.bind(self.panel):
            idle = 0
            self.started.set()  # persisted presence describes an already confirmed LOAD
            try:
                while not self.cancelled.is_set():
                    self.refresh()
                    with self.lock:
                        idle = idle + 1 if self.snapshot.get("player_state") == "IDLE" else 0
                    if idle >= 3:
                        break
                    self.cancelled.wait(3)
            finally:
                self.done.set()

    def prompt(self, callback: Callable, *args, **kwargs):
        request = Prompt(callback, args, kwargs)
        self.prompts.put(request)
        while not request.ready.wait(0.1):
            if self.cancelled.is_set() or not self.attached.is_set():
                request.ready.set()
                return None
        if request.error is not None:
            raise request.error
        return request.value

    def _work(self) -> None:
        with tui_output.bind(self.panel):
            self._run_worker()

    def _run_worker(self) -> None:
        try:
            with (
                util.cancel_scope(self.cancelled.is_set),
                tempfile.TemporaryDirectory(prefix="nstream-cast-", dir=util.runtime_dir()) as work,
            ):
                opts = self.opts
                for name in ("confirm", "choose", "choose_stream"):
                    callback = getattr(opts, name)
                    if callback is not None:
                        opts = replace(opts, **{name: self._menu_callback(callback)})
                start = self.kwargs.get("start")
                kwargs = {key: value for key, value in self.kwargs.items() if key != "start"}
                while True:
                    self.outcome = cast_flow.run_cast(
                        self.cfg,
                        self.results,
                        self.chosen,
                        device=self.device,
                        title=self.title,
                        opts=opts,
                        work_dir=work,
                        start=start,
                        follow=True,
                        allow_lang_switch=False,
                        on_event=self.receive,
                        cancelled=self.cancelled.is_set,
                        **kwargs,
                    )
                    if self.stop_requested.is_set() or self.cancelled.is_set():
                        self.outcome = replace(self.outcome, advance=False)
                    self.save(final=True)
                    with self.lock:
                        lang, start = self.next_audio, self.next_start
                        self.next_audio = None
                    if lang is None:
                        break
                    opts = replace(opts, audio_lang=lang)
                    self.started.clear()
                    self.stopping = False
                    self.panel.update(tui_output.Update("playback", "Cambio sorgente"))
        except KeyboardInterrupt:
            pass  # cooperative startup cancellation; delivery backends own cleanup
        except BaseException as exc:
            self.error = exc
            _log.exception("cast panel worker failed")
            message = (
                failures.describe(exc, title=self.title).message
                if isinstance(exc, failures.KNOWN)
                else str(exc)
            )
            tui_output.detail(message, warning=True)
        finally:
            self.save(final=True)
            self.done.set()

    def _menu_callback(self, callback: Callable) -> Callable:
        return lambda *args, **kwargs: self.prompt(callback, *args, **kwargs)

    def receive(self, event: dict) -> None:
        if self.cancelled.is_set() and event.get("kind") not in {"ended", "failed"}:
            raise KeyboardInterrupt
        kind = str(event.get("kind") or "")
        with self.lock:
            self.snapshot.update({k: v for k, v in event.items() if k != "kind"})
            if kind in {"playing", "paused"}:
                self.snapshot["player_state"] = "PAUSED" if kind == "paused" else "PLAYING"
        if event.get("delivery") == "mirror":
            self.mirror = True
        if kind == "started":
            self.started.set()
            self.panel.footer = self.help()
        label = {
            "started": "In onda",
            "playing": "In riproduzione",
            "paused": "In pausa",
            "ended": "Terminato",
            "failed": "Errore di riproduzione",
            "disconnected": "Monitoraggio disconnesso",
        }.get(kind)
        if label:
            self.panel.update(
                tui_output.Update("playback", label, "fail" if kind == "failed" else "done")
            )
        self._position()
        self.save()

    def _position(self) -> None:
        with self.lock:
            pos, dur = self.snapshot.get("position"), self.snapshot.get("duration")
        if pos is not None:
            text = f"{clock(pos)} / {clock(dur) if dur else 'durata sconosciuta'}"
            self.panel.update(tui_output.Update("position", text, "done"))

    def save(self, *, final: bool = False) -> None:
        with self.lock:
            pos, dur = self.snapshot.get("position") or 0, self.snapshot.get("duration") or 0
            if (
                self.on_save
                and pos > 0
                and dur > 0
                and (final or time.monotonic() - self.last_save > 15)
            ):
                self.last_save = time.monotonic()
                with contextlib.suppress(OSError):
                    self.on_save(pos, dur)

    def command(self, callback: Callable[[], None]) -> bool:
        if not self.command_lock.acquire(blocking=False):
            return False

        def run():
            with tui_output.bind(self.panel):
                try:
                    if not self.done.is_set():
                        callback()
                except Exception as exc:
                    tui_output.detail(str(exc), warning=True)
                finally:
                    self.command_lock.release()

        threading.Thread(target=run, name="cast-control", daemon=True).start()
        return True

    def poll(self) -> None:
        if self.remote or time.monotonic() - self.last_poll < 3:
            return
        if not self.poll_lock.acquire(blocking=False):
            return

        def refresh():
            with tui_output.bind(self.panel):
                try:
                    if not self.done.is_set():
                        self.refresh()
                finally:
                    self.poll_lock.release()

        threading.Thread(target=refresh, name="cast-status", daemon=True).start()

    def refresh(self) -> None:
        if self.mirror or time.monotonic() - self.last_poll < 3:
            return
        self.last_poll = time.monotonic()
        status = caster.status(self.device)
        state = status.get("player_state")
        with self.lock:
            # An unreachable receiver's normalized zeroes are not new playback evidence.
            self.snapshot.update(
                {
                    key: value
                    for key, value in status.items()
                    if state != "IDLE" or key not in {"position", "duration"}
                }
            )
        if state in {"PLAYING", "PAUSED", "BUFFERING"}:
            label = {"PLAYING": "In riproduzione", "PAUSED": "In pausa", "BUFFERING": "Buffering"}[
                state
            ]
            self.panel.update(tui_output.Update("playback", label, "done"))
        vol = status.get("volume_percent")
        if vol is not None:
            self.panel.update(tui_output.Update("volume", f"{vol}% Cast", "done"))
        if status.get("delivery_error"):
            tui_output.detail(f"Errore sorgente: {status['delivery_error']}", warning=True)
        self._position()
        self.save()

    def feedback(self, result: tuple[bool, str]) -> None:
        ok, text = result
        self.panel.update(tui_output.Update("activity", text, "done" if ok else "fail"))
        tui_output.detail(text, warning=not ok)

    def stop(self) -> None:
        self.stop_requested.set()  # suppress advancement even if delivery ends before stop replies
        with self.lock:
            self.next_audio = None
        ok, text = cast_control.stop_cast(self.cfg, device=self.device)
        if ok:
            self.stopping = True
            self.cancelled.set()
        self.feedback((ok, text))

    def control(self, key: str) -> None:
        key = {"\x1bOD": "\x1b[D", "\x1bOC": "\x1b[C"}.get(key, key)
        if key == "s":
            if not self.started.is_set():
                self.cancelled.set()
                return
            self.command(self.stop)
            return
        if key == "c" and self.started.is_set() and not self.mirror:
            self.command(
                lambda: self.feedback(handoff.transfer_to_local(self.cfg, device=self.device))
            )
            return
        if (
            not self.started.is_set()
            or self.mirror
            or key not in {" ", "\x1b[D", "\x1b[C", "+", "-"}
        ):
            return

        def apply():
            status = caster.status(self.device)
            if key == " ":
                command = "play" if status.get("player_state") == "PAUSED" else "pause"
                self.feedback(cast_control.media_control(command, device=self.device))
            elif key in {"\x1b[D", "\x1b[C"}:
                pos = status.get("position")
                if pos is None:
                    self.feedback((False, "Posizione non disponibile"))
                    return
                target = max(0, float(pos) + (-30 if key == "\x1b[D" else 30))
                dur = status.get("duration")
                if dur:
                    target = min(target, dur)
                self.feedback(cast_control.media_control("seek", value=target, device=self.device))
            elif key in {"+", "-"}:
                volume = status.get("volume_percent")
                if str(status.get("volume_control_type") or "").upper() == "FIXED":
                    self.feedback((False, "Il ricevitore non consente di modificare il volume"))
                    return
                if volume is None:
                    self.feedback((False, "Volume non disponibile"))
                    return
                target = max(0, min(100, volume + (5 if key == "+" else -5)))
                self.feedback(cast_control.set_cast_volume(target, device=self.device))

        if not self.command(apply):
            self.panel.update(tui_output.Update("activity", "Attendi il comando in corso…"))

    def audio(self) -> None:
        if self.mirror or not self.results or not self.started.is_set():
            return

        def choose():
            codes = cast_vet.cast_languages(
                self.cfg, self.results, exact_resolution=self.opts.quality or 0, title=self.title
            )
            if len(codes) < 2:
                self.feedback((False, "Nessun'altra lingua disponibile"))
                return
            lang = self.prompt(
                fzf,
                [(languages.name(c), c) for c in codes],
                "audio> ",
                header="Cambia sorgente audio dalla posizione attuale",
            )
            if not lang:
                return
            status = caster.status(self.device)
            start = status.get("position")
            if start is None:
                self.feedback((False, "Posizione non disponibile"))
                return
            # The existing domain flow vets the new dub, quality and P2P gate again.
            with self.lock:
                if self.done.is_set():
                    return
                self.next_audio, self.next_start = lang, float(start)
            ok, text = cast_control.stop_cast(self.cfg, device=self.device)
            if not ok:
                with self.lock:
                    self.next_audio = None
            self.feedback((ok, text))

        self.command(choose)

    def service_prompts(self) -> None:
        while True:
            try:
                request = self.prompts.get_nowait()
            except queue.Empty:
                return
            if request.ready.is_set():
                continue
            try:
                request.value = request.callback(*request.args, **request.kwargs)
            except BaseException as exc:
                request.error = exc
            finally:
                request.ready.set()

    def help(self) -> str:
        text = "Esc: home · s: ferma · d: dettagli"
        if not self.mirror:
            text = (
                "Esc: home · s: ferma · d: dettagli · Spazio: pausa · ←/→: ±30s · "
                "+/-: volume · c: computer"
            )
            if self.results:
                text += " · a: audio"
        return text

    def issue(self) -> str | None:
        if self.error:
            return (
                failures.describe(self.error, title=self.title).message
                if isinstance(self.error, failures.KNOWN)
                else str(self.error)
            )
        error = self.outcome.cast_error if self.outcome else None
        error = error or self.snapshot.get("error")
        if error:
            return f"Riproduzione interrotta ({error})"
        return self.outcome.notice if self.outcome else None

    def open(self) -> bool:
        """Return True for an explicit return home, False when the backend finishes."""
        self.attached.set()
        try:
            with tui_output.focus(self.panel):
                while not self.done.is_set():
                    self.service_prompts()
                    if self.started.is_set():
                        self.panel.footer = self.help()
                        self.poll()
                    tui_output.render()
                    try:
                        key = read_key()
                    except KeyboardInterrupt:
                        key = "\x1b"
                    if key == "\x1b":
                        if self.started.is_set():
                            return True
                        self.cancelled.set()
                        self.panel.update(tui_output.Update("playback", "Annullamento…"))
                    elif key == "d":
                        with self.panel.lock:
                            rows = [self.title, self.device]
                            rows += [
                                f"{key}: {value.text}" for key, value in self.panel.rows.items()
                            ]
                            rows += list(self.panel.details)
                        rows += self.help().split(" · ")
                        rows = [tui_output.plain(row) for row in rows]
                        fzf_index(
                            rows,
                            prompt="dettagli> ",
                            header="Stato, avvisi e comandi",
                            help_previews=rows,
                        )
                    elif key == "a":
                        self.audio()
                    elif key is not None:
                        self.control(key)
                    if self.stopping:
                        return True
                if self.error:
                    # Keep failure text visible in the caller's next menu.
                    return False
                return False
        finally:
            self.attached.clear()


_active: CastTask | None = None


def active() -> CastTask | None:
    return _active if _active is not None and not _active.done.is_set() else None


def play(
    cfg: Config, results: list[Stream], chosen: Stream, **kwargs
) -> cast_flow.CastOutcome | None:
    global _active
    previous = active()
    if previous is not None:
        if not previous.command_lock.acquire(timeout=1):
            raise CastBusy("Un comando del cast è in corso; riprova tra qualche secondo")
        try:
            previous.stop()
        finally:
            previous.command_lock.release()
        previous.thread.join(timeout=1)
        if not previous.done.is_set():
            raise CastBusy("Il cast precedente si sta chiudendo; riprova tra qualche secondo")
    panel = tui_output.current() or tui_output.Panel(kwargs["title"])
    panel.destination = kwargs["device"]
    task = CastTask(cfg, results, chosen, panel=panel, **kwargs)
    _active = task
    task.attached.set()
    task.thread.start()
    if task.open():
        raise HomeRequested
    task.reported = True
    if task.error is not None:
        raise task.error
    issue = task.issue()
    return replace(task.outcome, notice=issue) if task.outcome and issue else task.outcome


def reopen() -> bool:
    task = active()
    if task is None:
        return False
    task.open()
    return True


def watch_existing(cfg: Config, session: dict) -> None:
    """Open a cast launched elsewhere without loading its media a second time."""
    global _active
    title, device = session.get("title") or "Cast", session.get("device") or ""
    if not device:
        return
    opts = PlayOpts(
        auto=True, cast=True, sub_mode=None, sub_lang=None, history=False, autoplay=False
    )
    panel = tui_output.Panel(title, device)
    panel.update(tui_output.Update("playback", "Aggiorno stato…"))
    task = CastTask(cfg, [], {}, device=device, title=title, opts=opts, panel=panel, remote=True)
    _active = task
    task.thread.start()
    task.open()


def finished_notice() -> str | None:
    """Surface a background failure once when a menu next redraws."""
    if _active is None or not _active.done.is_set() or _active.reported:
        return None
    _active.reported = True
    return _active.issue()
