"""Tests for the shared low-level helpers."""

from __future__ import annotations

import json
import stat

import pytest

from nstream import util


def test_atomic_write_creates_file_and_dir(tmp_path):
    path = tmp_path / "sub" / "out.json"
    util.atomic_write(path, lambda f: json.dump({"a": 1}, f), prefix=".out-")
    assert json.loads(path.read_text()) == {"a": 1}
    # 0600 perms, parent dir auto-created.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_write_replaces_existing(tmp_path):
    path = tmp_path / "out.json"
    path.write_text("old")
    util.atomic_write(path, lambda f: f.write("new"), prefix=".out-")
    assert path.read_text() == "new"


def test_atomic_write_no_temp_left_on_success(tmp_path):
    path = tmp_path / "out.json"
    util.atomic_write(path, lambda f: f.write("x"), prefix=".out-")
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_cleans_temp_and_raises_on_error(tmp_path):
    path = tmp_path / "out.json"

    def boom(_f):
        raise OSError("disk full")

    with pytest.raises(OSError):
        util.atomic_write(path, boom, prefix=".out-")
    assert list(tmp_path.glob("*.tmp")) == []  # temp cleaned up
    assert not path.exists()  # target untouched


def test_atomic_write_bytes_roundtrip(tmp_path):
    path = tmp_path / "sub" / "poster.img"
    util.atomic_write_bytes(path, b"\x89PNG\x00", prefix=".poster-")
    assert path.read_bytes() == b"\x89PNG\x00"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.glob("**/*.tmp")) == []


def test_load_json_roundtrip(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"k": "v"}))
    assert util.load_json(path, {}) == {"k": "v"}


def test_load_json_missing_returns_fallback(tmp_path):
    assert util.load_json(tmp_path / "nope.json", {"default": True}) == {"default": True}


def test_load_json_corrupt_returns_fallback(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert util.load_json(path, {}) == {}


def test_load_json_wrong_type_returns_fallback(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")  # a list where a dict was expected
    assert util.load_json(path, {}) == {}


def test_run_cmd_success():
    proc = util.run_cmd(["printf", "hello"])
    assert proc is not None
    assert proc.stdout == "hello"


def test_run_cmd_missing_binary_returns_none():
    assert util.run_cmd(["nstream-definitely-not-a-binary-xyz"]) is None


def test_run_cmd_timeout_returns_none():
    assert util.run_cmd(["sleep", "5"], timeout=0.1) is None


def test_run_cmd_passes_input():
    proc = util.run_cmd(["cat"], input="piped")
    assert proc is not None
    assert proc.stdout == "piped"
