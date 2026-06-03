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
    assert cfg.auto_play is True
    assert cfg.autoplay is True
    assert cfg.autoplay_lead == 15
    assert cfg.lang_filter is True
    assert cfg.exclude_camrip is True
    assert cfg.min_seeders == 3
    assert cfg.dedup is True
    assert cfg.max_streams == 20
    assert cfg.mpv_args == []


def test_stream_filter_overrides_and_clamp(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(
        tmp_path,
        {
            "torrentio_base": "tb",
            "lang_filter": False,
            "exclude_camrip": False,
            "dedup": False,
            "min_seeders": -5,  # clamped to 0
            "max_streams": "bad",  # falls back to default
        },
    )
    cfg = config.load()
    assert cfg.lang_filter is False and cfg.exclude_camrip is False and cfg.dedup is False
    assert cfg.min_seeders == 0
    assert cfg.max_streams == config.Config.max_streams


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


def test_audio_langs_and_addons_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb"})
    cfg = config.load()
    assert cfg.audio_langs == ["ita", "eng"]
    assert cfg.addons == []


def test_audio_langs_and_addons_override(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(
        tmp_path,
        {"torrentio_base": "tb", "audio_langs": ["jpn"], "addons": ["https://x/manifest.json"]},
    )
    cfg = config.load()
    assert cfg.audio_langs == ["jpn"]
    assert cfg.addons == ["https://x/manifest.json"]


def test_save_merges_and_preserves_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", "custom": "keep"})
    config.save({"audio_langs": ["jpn", "eng"]})
    raw = json.loads((tmp_path / "nstream" / "config.json").read_text())
    assert raw["custom"] == "keep"  # unknown key preserved
    assert raw["audio_langs"] == ["jpn", "eng"]
    assert raw["torrentio_base"] == "tb"


def test_save_chmod_600(tmp_path, monkeypatch):
    import stat

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save({"torrentio_base": "tb"})
    mode = stat.S_IMODE((tmp_path / "nstream" / "config.json").stat().st_mode)
    assert mode == 0o600


def test_save_no_leftover_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.save({"torrentio_base": "tb"})
    assert list((tmp_path / "nstream").glob(".config-*.tmp")) == []


def test_hw_filter_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb"})
    cfg = config.load()
    assert cfg.hw_filter is True
    assert cfg.max_resolution == 2160
    assert cfg.allow_software is False and cfg.allow_dv5 is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1080, 1080), (0, 0), (-5, 0), ("bad", 2160)],  # negative clamped to 0, invalid → default
)
def test_max_resolution_coercion(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", "max_resolution": value})
    assert config.load().max_resolution == expected
