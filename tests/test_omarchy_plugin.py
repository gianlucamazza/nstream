"""The Omarchy bar widget in integrations/omarchy-nstream: manifest, catalogs, helper."""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "integrations" / "omarchy-nstream"
OMARCHY_SHELL = pathlib.Path("/usr/share/omarchy/shell")
WAYLAND_SOCKET = pathlib.Path(
    os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
) / os.environ.get("WAYLAND_DISPLAY", "missing")


def test_manifest_identity_and_entry_point():
    m = json.loads((PLUGIN / "manifest.json").read_text())
    assert m["schemaVersion"] == 1 and m["id"] == "gianlucamazza.nstream"
    assert m["kinds"] == ["bar-widget"]
    assert (PLUGIN / m["entryPoints"]["barWidget"]).is_file()
    assert not any(p.is_symlink() for p in PLUGIN.rglob("*"))  # omarchy plugin add refuses links


def test_catalogs_share_keys_and_cover_the_panel():
    catalogs = {p.stem: json.loads(p.read_text()) for p in (PLUGIN / "i18n").glob("*.json")}
    assert {"en", "it"} <= catalogs.keys()
    keys = set(catalogs["en"])
    for lang, cat in catalogs.items():
        assert set(cat) == keys, lang
    used = set(re.findall(r"tr\.tn?\(\"([^\"]+)\"", (PLUGIN / "Panel.qml").read_text()))
    assert used and used <= keys, used - keys


def test_panel_text_is_plain():
    qml = (PLUGIN / "Panel.qml").read_text()
    assert "textFormat: Text.PlainText" in qml


def test_helper_unit_tests_pass():
    # Own process: the plugin's `tests` package would collide with this repo's.
    p = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
        cwd=PLUGIN, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert p.returncode == 0, p.stderr[-2000:]


@pytest.mark.skipif(
    not shutil.which("quickshell") or not OMARCHY_SHELL.is_dir() or not WAYLAND_SOCKET.exists(),
    reason="requires an installed Omarchy shell and a Wayland connection",
)
def test_panel_runtime_errors_and_notification_markup(tmp_path, monkeypatch):
    """Load the real shell components, with fake CLI/notifications and no visible panel."""
    config = tmp_path / "shell"
    config.mkdir()
    for entry in OMARCHY_SHELL.iterdir():
        if entry.name != "shell.qml":
            (config / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    shutil.copytree(PLUGIN, config / "widget")
    (config / "widget" / "helper.py").write_text(
        'import json, sys\nprint(json.dumps({"kind":"history","ok":True,"entries":[],"session":None}) '
        'if sys.argv[1] == "history" else "invalid JSON")\n'
    )
    notification = tmp_path / "notification.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    notify = bin_dir / "notify-send"
    notify.write_text(
        f"#!{sys.executable}\nimport json, pathlib, sys\n"
        f"pathlib.Path({str(notification)!r}).write_text(json.dumps(sys.argv[1:]))\n"
    )
    notify.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("WAYLAND_DISPLAY", str(WAYLAND_SOCKET))
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent")
    (config / "shell.qml").write_text("""import QtQuick
import Quickshell
import "widget"
ShellRoot {
  Panel { id: widget }
  Timer { interval: 500; running: true; onTriggered: {
    if (widget.historyRaw === "" || widget.historyError !== "") throw new Error("history process failed");
    widget.starting = "Test";
    widget.receive("invalid JSON", "continue");
    if (widget.starting !== "" || !widget.messageIsError) throw new Error("stuck continue");
    widget.session = {title:"Test",device:"TV"};
    widget.receive(JSON.stringify({kind:"status",ok:false,error:"receiver failed"}), "status");
    if (widget.statusError !== "receiver failed") throw new Error("hidden status error");
    widget.localTitle = "Test";
    widget.receive(JSON.stringify({kind:"local",ok:false,error:"no streams"}), "local");
    if (widget.localTitle !== "" || widget.message !== "no streams") throw new Error("hidden local failure");
    widget.target = "local"; widget.resumeEntry({title:"Test"});
  } }
  Timer { interval: 1000; running: true; onTriggered: {
    if (widget.localTitle !== "" || !widget.messageIsError) throw new Error("stuck local process");
    widget.notify("Test", '<img src="https://example.invalid/tracker">& --help');
  } }
  Timer { interval: 1400; running: true; onTriggered: {
    console.log("NSTREAM_QML_CHECKED"); Qt.quit();
  } }
}
""")
    proc = subprocess.run(
        ["quickshell", "-p", str(config), "--no-color"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "NSTREAM_QML_CHECKED" in output, output[-4000:]
    assert "Error:" not in output, output[-4000:]
    argv = json.loads(notification.read_text())
    assert argv[-3:] == [
        "--",
        "Test",
        '&lt;img src="https://example.invalid/tracker"&gt;&amp; --help',
    ]
