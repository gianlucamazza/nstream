"""The Omarchy bar widget in integrations/omarchy-nstream: manifest, catalogs, helper."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "integrations" / "omarchy-nstream"


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
