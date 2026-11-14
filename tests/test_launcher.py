"""Single-window desktop activation, including simultaneous launches."""

import fcntl
import json
import os
import subprocess
import sys
import time

from nstream import launcher


def test_focus_existing_checks_app_identity_and_uses_exact_address(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test")
    monkeypatch.setattr(launcher.shutil, "which", lambda cmd: "/usr/bin/" + cmd)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "clients":
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {"class": "browser", "title": "nstream", "address": "0x10"},
                        {"class": "nstream", "title": "Home", "address": "0x20"},
                    ]
                ),
            )
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.focus_existing()
    assert calls[-1] == ["hyprctl", "dispatch", 'hl.dsp.focus({ window = "address:0x20" })']


def test_busy_launcher_without_compositor_never_spawns_second_window(monkeypatch, tmp_path):
    path = tmp_path / "launcher.lock"
    with path.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        monkeypatch.setattr(launcher, "_lock_path", lambda: path)
        monkeypatch.setattr(launcher, "_activate", lambda owner=None: None)
        ticks = iter([0, 9])
        monkeypatch.setattr(launcher.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(
            launcher.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn a duplicate")),
        )
        assert launcher._run() == 1


def test_lock_symlink_is_rejected_without_modifying_target(monkeypatch, tmp_path):
    target = tmp_path / "target"
    target.write_text("keep")
    lock = tmp_path / "lock"
    lock.symlink_to(target)
    monkeypatch.setattr(launcher, "_lock_path", lambda: lock)
    assert launcher._run() == 1
    assert target.read_text() == "keep"


def test_real_concurrent_launches_share_window_and_lock_releases(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    active = tmp_path / "active"
    count = tmp_path / "count"
    release = tmp_path / "release"
    foot = bin_dir / "foot"
    foot.write_text(f"""#!{sys.executable}
from pathlib import Path
import time
active=Path({str(active)!r}); release=Path({str(release)!r})
with Path({str(count)!r}).open('a') as f: f.write('window\\n')
active.write_text(str(__import__("os").getpid()))
try:
 deadline=time.monotonic()+10
 while not release.exists() and time.monotonic()<deadline: time.sleep(.01)
finally:
 active.unlink(missing_ok=True)
""")
    foot.chmod(0o755)
    hypr = bin_dir / "hyprctl"
    hypr.write_text(f"""#!{sys.executable}
import json,sys
from pathlib import Path
if sys.argv[1]=='clients':
 print(json.dumps([{{'class':'nstream','address':'0x10','pid':int(Path({str(active)!r}).read_text())}}] if Path({str(active)!r}).exists() else []))
""")
    hypr.chmod(0o755)
    env = dict(
        os.environ,
        XDG_RUNTIME_DIR=str(runtime),
        HYPRLAND_INSTANCE_SIGNATURE="test",
        PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
    )
    command = [sys.executable, "-c", "from nstream.launcher import main; raise SystemExit(main())"]
    first = subprocess.Popen(command, env=env)
    try:
        deadline = time.monotonic() + 5
        while not active.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert active.exists(), "first window did not start"
        second = subprocess.run(command, env=env, timeout=5)
        assert second.returncode == 0
        assert count.read_text().splitlines() == ["window"]
        release.touch()
        assert first.wait(timeout=5) == 0
        # Lock files may remain, but their kernel lock must not outlive the terminal.
        release.unlink()
        third = subprocess.run(
            [sys.executable, "-m", "nstream.launcher", "--json"],
            env=env,
            timeout=5,
            capture_output=True,
            text=True,
        )
        assert third.returncode == 0
        assert json.loads(third.stdout)["result"] == "opened"
        assert count.read_text().splitlines() == ["window", "window"]
        release.touch()
        deadline = time.monotonic() + 5
        while active.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not active.exists()
    finally:
        release.touch()
        first.wait(timeout=5)


def test_interactive_instance_focuses_owner_and_releases_on_exit(monkeypatch, capsys):
    focused = []
    monkeypatch.setattr(
        launcher, "focus_existing", lambda owner=None: focused.append(owner) or True
    )
    with launcher.interactive_instance() as first:
        assert first
        with launcher.interactive_instance() as second:
            assert not second
        assert focused == [os.getpid()]
    with launcher.interactive_instance() as third:
        assert third
    assert "già aperta" in capsys.readouterr().err


def test_interactive_instance_disk_failure_is_best_effort(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "_lock_path", lambda: tmp_path / "missing" / "lock")
    with launcher.interactive_instance() as acquired:
        assert acquired


def test_failed_activation_of_known_window_does_not_spawn_duplicate(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "test")
    monkeypatch.setattr(launcher.shutil, "which", lambda command: "/usr/bin/" + command)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert args[0] == "hyprctl", "must not create another terminal"
        if args[1] == "clients":
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {"class": "nstream", "address": "0x10"},
                    ]
                ),
            )
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    replies = []
    assert launcher._run(replies.append) == 1
    assert replies[0]["error"] == "focus_failed"
    assert calls[-1] == ["hyprctl", "dispatch", "focuswindow", "address:0x10"]


def test_interactive_guard_works_without_omarchy_compositor_or_terminal_helper(monkeypatch):
    for key in ("HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "DISPLAY", "OMARCHY_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda command: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("TUI guard must not start a desktop process")

    monkeypatch.setattr(launcher.subprocess, "run", forbidden)
    with launcher.interactive_instance() as first:
        assert first
        with launcher.interactive_instance() as second:
            assert not second


def test_immediate_terminal_failure_is_reported(monkeypatch):
    monkeypatch.setattr(launcher, "_activate", lambda owner=None: None)
    monkeypatch.setattr(launcher, "_owner", lambda: None)
    monkeypatch.setattr(launcher, "_window_visible", lambda pid: False)
    monkeypatch.setattr(launcher.shutil, "which", lambda command: "/usr/bin/false")
    reports = []
    assert launcher._run(reports.append) == 1
    assert reports == [launcher._reply("launch_failed")]


def test_json_launcher_help_does_not_create_a_window():
    result = subprocess.run(
        [sys.executable, "-m", "nstream.launcher", "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert "--json" in result.stdout
