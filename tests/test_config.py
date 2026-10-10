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
    assert cfg.prefer_cast is False
    assert cfg.cast_device == ""
    assert cfg.cast_receiver_app_id == ""
    assert cfg.cast_lan_proxy is True
    assert cfg.autoplay is True
    assert cfg.autoplay_lead == 15
    assert cfg.lang_filter is True
    assert cfg.exclude_camrip is True
    assert cfg.min_seeders == 3
    assert cfg.dedup is True
    assert cfg.max_streams == 20
    assert cfg.default_quality is None
    assert cfg.home_continue_max == 12
    assert cfg.mpv_args == []
    assert cfg.nerd_font == "auto"
    assert cfg.posters is True
    assert cfg.image_mode == "auto"
    assert cfg.torrentio_enabled is True
    assert cfg.trakt_addon == ""


def test_load_tightens_world_readable_config(tmp_path, monkeypatch):
    import stat

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "realdebrid=SECRET"})
    path = tmp_path / "nstream" / "config.json"
    path.chmod(0o644)
    config.load()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.load().torrentio_base == "realdebrid=SECRET"


def test_default_quality_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", "default_quality": 1080})
    assert config.load().default_quality == 1080
    write_config(tmp_path, {"torrentio_base": "tb", "default_quality": 0})
    assert config.load().default_quality == 0
    write_config(tmp_path, {"torrentio_base": "tb", "default_quality": None})
    assert config.load().default_quality is None


def test_ui_fields_override_and_validate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(
        tmp_path,
        {
            "torrentio_base": "tb",
            "nerd_font": "on",
            "posters": False,
            "image_mode": "bogus",  # not in the allowlist → default "auto"
        },
    )
    cfg = config.load()
    assert cfg.nerd_font == "on"
    assert cfg.posters is False
    assert cfg.image_mode == "auto"


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


def test_missing_torrentio_base_defaults_token_less(tmp_path, monkeypatch):
    # A config without torrentio_base is valid now: the default is token-less so the
    # local P2P backend works out of the box (no paid debrid key required).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"cinemeta": "x"})
    cfg = config.load()
    assert cfg.torrentio_base == "sort=qualitysize"
    assert cfg.playback_backend == "local"


def test_trakt_addon_from_config_and_env(tmp_path, monkeypatch):
    """ADR 0049: trakt_addon is config/env only; env wins. Not an indexer key."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("NSTREAM_TRAKT_ADDON", raising=False)
    write_config(tmp_path, {"torrentio_base": "tb", "trakt_addon": "https://t/manifest.json"})
    assert config.load().trakt_addon == "https://t/manifest.json"
    monkeypatch.setenv("NSTREAM_TRAKT_ADDON", "https://env/manifest.json")
    assert config.load().trakt_addon == "https://env/manifest.json"
    write_config(tmp_path, {"torrentio_base": "tb", "trakt_addon": None})
    monkeypatch.delenv("NSTREAM_TRAKT_ADDON", raising=False)
    assert config.load().trakt_addon == ""
    write_config(tmp_path, {"torrentio_base": "tb", "trakt_addon": ["nope"]})
    with pytest.raises(config.ConfigError, match="trakt_addon"):
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


def test_torrentio_enabled_default_and_override(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb"})
    assert config.load().torrentio_enabled is True
    write_config(tmp_path, {"torrentio_base": "tb", "torrentio_enabled": False})
    assert config.load().torrentio_enabled is False


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
    [
        (1080, 1080),
        (0, 0),  # 0 = no cap, kept
        (-5, 0),  # negative clamped to 0
        (99999, 4320),  # clamped down to the 8K ceiling
        ("bad", 2160),  # invalid → default
    ],
)
def test_max_resolution_coercion(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", "max_resolution": value})
    assert config.load().max_resolution == expected


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("min_seeders", 999, 100),  # clamped down to ceiling
        ("max_streams", 999, 500),  # clamped down to ceiling
    ],
)
def test_int_fields_clamped_to_ceiling(tmp_path, monkeypatch, key, value, expected):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", key: value})
    assert getattr(config.load(), key) == expected


def test_cast_mirror_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb"})
    cfg = config.load()
    assert cfg.cast_mode == "dmr"
    assert cfg.mirror_bitrate == 0
    assert cfg.mirror_playout_ms == 0
    assert cfg.cast_mirror_over_remux_gb == 10  # ADR 0015 default threshold


def test_cast_mirror_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(
        tmp_path,
        {
            "torrentio_base": "tb",
            "cast_mode": "mirror",
            "mirror_bitrate": 8_000_000,
            "mirror_playout_ms": 300,
        },
    )
    cfg = config.load()
    assert cfg.cast_mode == "mirror"
    assert cfg.mirror_bitrate == 8_000_000
    assert cfg.mirror_playout_ms == 300


def test_cast_mode_invalid_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", "cast_mode": "bogus"})
    assert config.load().cast_mode == "dmr"


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("mirror_bitrate", -1, 0),  # clamped up
        ("mirror_bitrate", 999_999_999, 100_000_000),  # clamped down to ceiling
        ("mirror_bitrate", "bad", 0),  # invalid → default
        ("mirror_playout_ms", -1, 0),  # clamped up
        ("mirror_playout_ms", 99999, 5000),  # clamped down to ceiling
        ("mirror_playout_ms", "bad", 0),  # invalid → default
        ("cast_mirror_over_remux_gb", -1, 0),  # clamped up (0 = disabled)
        ("cast_mirror_over_remux_gb", 9999, 1000),  # clamped down to ceiling
        ("cast_mirror_over_remux_gb", "bad", 10),  # invalid → default
    ],
)
def test_mirror_int_coercion(tmp_path, monkeypatch, key, value, expected):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write_config(tmp_path, {"torrentio_base": "tb", key: value})
    assert getattr(config.load(), key) == expected


def test_non_dict_root_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    d = tmp_path / "nstream"
    d.mkdir(parents=True)
    (d / "config.json").write_text('["not", "an", "object"]')
    with pytest.raises(config.ConfigError):
        config.load()


def test_primary_and_fallback_langs():
    from nstream.config import Config

    assert Config().primary == "ita"  # default: first audio lang
    assert Config().fallback_langs == ["eng"]
    c = Config(audio_langs=["eng", "ita", "fra"])
    assert c.primary == "eng" and c.fallback_langs == ["ita", "fra"]
    # explicit primary_lang overrides the first-of-audio_langs derivation
    c2 = Config(audio_langs=["eng", "ita"], primary_lang="ita")
    assert c2.primary == "ita" and c2.fallback_langs == ["eng"]


def test_sub_align_keys_round_trip_through_load(tmp_path, monkeypatch):
    """ADR 0020 keys MUST be parsed by load() — the ADR-0019 keys existed on the
    dataclass but the loader never read them, so the opt-in was unreachable from
    config.json (found in the ADR-0020 exploration). Never again."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"torrentio_base": "tb", "sub_align": false, "sub_align_budget_s": 5000}')
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    cfg = config.load()
    assert cfg.sub_align is False
    assert cfg.sub_align_budget_s == 600  # clamped to INT_BOUNDS (60, 600)


def test_sub_align_defaults(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"torrentio_base": "tb", "sub_autosync": true}')  # stale key: inert
    monkeypatch.setattr(config, "config_path", lambda: cfg_file)
    cfg = config.load()
    assert cfg.sub_align is True and cfg.sub_align_budget_s == 240
