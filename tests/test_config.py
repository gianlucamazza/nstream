"""Unit tests for config loading and coercion."""

from __future__ import annotations

import json

import pytest

from nstream import config


def write_config(tmp_path, data) -> None:
    d = tmp_path / "nstream"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(data))


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb"})
    cfg = config.load()
    assert cfg.torrentio_base == "tb"
    assert cfg.cinemeta == config.Config.cinemeta
    assert cfg.subtitle_langs == ["ita", "eng"]
    assert cfg.history_enabled is True
    assert cfg.hwdec == "auto-safe"
    assert cfg.autoplay is True
    assert cfg.autoplay_lead == 15
    assert cfg.mpv_args == []


def test_missing_torrentio_base_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"cinemeta": "x"})
    with pytest.raises(config.ConfigError):
        config.load()


def test_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(config.ConfigError):
        config.load()


def test_invalid_json_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "nstream"
    d.mkdir(parents=True)
    (d / "config.json").write_text("{ not json")
    with pytest.raises(config.ConfigError):
        config.load()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({}, "auto-safe"),  # absent → default
        ({"hwdec": ""}, ""),  # explicit empty → disabled
        ({"hwdec": False}, ""),  # false → disabled
        ({"hwdec": None}, ""),  # null → disabled
        ({"hwdec": "vaapi"}, "vaapi"),  # explicit value kept
    ],
)
def test_hwdec_coercion(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", **raw})
    assert config.load().hwdec == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 1),  # clamped up
        (-5, 1),  # clamped up
        (200, 120),  # clamped down
        (30, 30),  # kept
        ("bad", 15),  # invalid → default
    ],
)
def test_autoplay_lead_clamp(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", "autoplay_lead": value})
    assert config.load().autoplay_lead == expected


def test_autoplay_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", "autoplay": False})
    assert config.load().autoplay is False
