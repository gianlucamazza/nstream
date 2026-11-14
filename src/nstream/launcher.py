"""Optional desktop launcher and independent TUI instance guard (ADR 0058)."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import log, util


def _activate(owner: int | None = None) -> str | None:
    """Activate our app ID, with a migration fallback for older foot launchers."""
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
        return None
    try:
        result = subprocess.run(
            ["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2, check=True
        )
        clients = json.loads(result.stdout)
        if not isinstance(clients, list):
            return None
        ancestors: set[int] = set()
        pid = owner
        while pid and pid not in ancestors and len(ancestors) < 64:
            ancestors.add(pid)
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                pid = int(fields[1])
            except (OSError, ValueError, IndexError):
                break
        for client in clients:
            if not isinstance(client, dict):
                continue
            own = client.get("pid") in ancestors if owner else client.get("class") == "nstream"
            legacy = (
                not owner and client.get("class") == "foot" and client.get("title") == "nstream"
            )
            address = str(client.get("address") or "")
            if (own or legacy) and re.fullmatch(r"0x[0-9a-fA-F]+", address):
                # Lua-configured Hyprland uses a dispatcher expression; older versions
                # accept the legacy string command. The address is strictly validated.
                commands = [
                    ["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{address}" }})'],
                    ["hyprctl", "dispatch", "focuswindow", f"address:{address}"],
                ]
                for command in commands:
                    try:
                        focused = subprocess.run(command, capture_output=True, timeout=2)
                        if focused.returncode == 0:
                            return "focused"
                    except (OSError, subprocess.SubprocessError):
                        continue
                # Never create a competitor just because window activation failed.
                return "focus_failed"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def focus_existing(owner: int | None = None) -> bool:
    return _activate(owner) == "focused"


def _lock_path() -> Path:
    # Scope the singleton to this graphical session, allowing independent user displays.
    display = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY") or "default"
    scope = hashlib.sha256(display.encode()).hexdigest()[:16]
    return util.runtime_dir() / f"nstream-launch-{os.getuid()}-{scope}.lock"


@contextlib.contextmanager
def interactive_instance():
    """Serialize interactive CLI sessions; JSON clients do not acquire this lock."""
    fd = None
    try:
        path = _lock_path().with_suffix(".tui.lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            metadata = os.fstat(fd)
            if metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode):
                raise OSError("unsafe TUI lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            assert fd is not None
            with contextlib.suppress(OSError, ValueError, AttributeError, TypeError):
                os.lseek(fd, 0, os.SEEK_SET)
                info = json.loads(os.read(fd, 256))
                pid = info.get("pid")
                if isinstance(pid, int) and pid > 0 and util.proc_start(pid) == info.get("start"):
                    focus_existing(pid)
            print("nstream: una sessione interattiva è già aperta", file=sys.stderr)
            yield False
            return
        except OSError:
            # Disk I/O remains best-effort: an unavailable runtime dir cannot block playback.
            yield True
            return
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.write(
                fd, json.dumps({"pid": os.getpid(), "start": util.proc_start(os.getpid())}).encode()
            )
        yield True
    finally:
        if fd is not None:
            os.close(fd)


_READY_TIMEOUT = 8.0
_ACK_TIMEOUT = 12.0


def _owner() -> int | None:
    """Return a verified live TUI owner; an unlocked stale file is not presence."""
    fd = None
    try:
        fd = os.open(
            _lock_path().with_suffix(".tui.lock"), os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        if os.fstat(fd).st_uid != os.getuid():
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
        except BlockingIOError:
            info = json.loads(os.read(fd, 256))
            pid = info.get("pid")
            start = info.get("start")
            if (
                isinstance(pid, int)
                and pid > 0
                and start is not None
                and util.proc_start(pid) == start
            ):
                return pid
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    finally:
        if fd is not None:
            os.close(fd)
    return None


def _descends_from(pid: int, ancestor: int) -> bool:
    seen = set()
    while pid > 1 and pid not in seen and len(seen) < 64:
        if pid == ancestor:
            return True
        seen.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def _window_visible(pid: int) -> bool:
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
        return False
    try:
        proc = subprocess.run(
            ["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2, check=True
        )
        windows = json.loads(proc.stdout)
        return isinstance(windows, list) and any(
            isinstance(w, dict) and w.get("pid") == pid and w.get("class") == "nstream"
            for w in windows
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _reply(result: str) -> dict:
    ok = result in ("opened", "focused", "already_open")
    return {
        "api_version": 1,
        "action": "launch",
        "ok": ok,
        "result": result if ok else None,
        "error": None if ok else result,
    }


def _run(report=None) -> int:
    """Supervise the terminal while reporting one bounded activation acknowledgement."""
    fd = None
    terminal = None
    reported = False

    def acknowledge(result: str) -> int:
        nonlocal reported
        if not reported:
            reported = True
            if report:
                report(_reply(result))
        return 0 if _reply(result)["ok"] else 1

    try:
        fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        metadata = os.fstat(fd)
        if metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode):
            raise OSError("unsafe launcher lock")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            deadline = time.monotonic() + _READY_TIMEOUT
            while time.monotonic() < deadline:
                owner = _owner()
                activation = _activate(owner)
                if activation:
                    return acknowledge(activation)
                if owner:
                    return acknowledge("already_open")
                time.sleep(0.1)
            return acknowledge("launch_busy")
        owner = _owner()
        activation = _activate(owner)
        if activation:
            return acknowledge(activation)
        if owner:
            return acknowledge("already_open")
        foot = shutil.which("foot")
        if foot is None:
            return acknowledge("terminal_missing")
        command = (
            '"$@"; ec=$?; if [ "$ec" -ne 0 ]; then '
            'printf "\\nnstream: uscita %s — INVIO per chiudere.\\n" "$ec"; '
            'read -r _; fi; exit "$ec"'
        )
        terminal = subprocess.Popen(
            [
                foot,
                "--app-id=nstream",
                "--term=foot",
                "-T",
                "nstream",
                "sh",
                "-c",
                command,
                "nstream",
                sys.executable,
                "-m",
                "nstream",
            ]
        )
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if terminal.poll() is not None:
                owner = _owner()
                if owner:
                    return acknowledge(_activate(owner) or "already_open")
                return acknowledge("launch_failed")
            # A window proves visible activation; a new core owner proves readiness on
            # compositors where window inspection is unavailable.
            owner = _owner()
            if _window_visible(terminal.pid) or (owner and _descends_from(owner, terminal.pid)):
                acknowledge("opened")
                return terminal.wait()
            time.sleep(0.05)
        acknowledge("launch_timeout")
        # Keep supervising an unconfirmed terminal. A timeout must not orphan it or
        # release the singleton lock while it may still become ready.
        return terminal.wait()
    except (OSError, subprocess.SubprocessError):
        return acknowledge("launch_failed")
    finally:
        if terminal is not None and terminal.poll() is None:
            # Closing the observation pipe never terminates the user's terminal.
            terminal.wait()
        if fd is not None:
            os.close(fd)


def _worker(fd: int) -> int:
    def report(result: dict) -> None:
        nonlocal fd
        if fd < 0:
            return
        try:
            os.write(fd, (json.dumps(result) + "\n").encode())
        except OSError:
            pass
        finally:
            os.close(fd)
            fd = -1

    try:
        return _run(report)
    finally:
        if fd >= 0:
            os.close(fd)


def _request() -> dict:
    read_fd, write_fd = os.pipe()
    try:
        subprocess.Popen(
            [sys.executable, "-m", "nstream.launcher", "--report-fd", str(write_fd)],
            pass_fds=(write_fd,),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(write_fd)
        write_fd = -1
        if not select.select([read_fd], [], [], _ACK_TIMEOUT)[0]:
            return _reply("launch_timeout")
        data = os.read(read_fd, 4096)
        result = json.loads(data)
        if (
            isinstance(result, dict)
            and result.get("api_version") == 1
            and result.get("action") == "launch"
            and type(result.get("ok")) is bool
        ):
            return result
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    return _reply("launch_failed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nstream-launch")
    parser.add_argument("--json", action="store_true", help="bounded activation acknowledgement")
    parser.add_argument("--report-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.report_fd is not None:
        return _worker(args.report_fd)
    if args.json:
        result = _request()
        log.emit_json(result)
        return 0 if result["ok"] else 1
    return _run(
        lambda result: (
            print("nstream: " + str(result["error"]), file=sys.stderr) if not result["ok"] else None
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
