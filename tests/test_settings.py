"""Unit tests for settings helpers (pure parts; the fzf UI needs a tty)."""

from __future__ import annotations

from nstream import settings
from nstream.config import Config


def test_token_status():
    assert (
        settings._token_status(Config(torrentio_base="sort=x|realdebrid=ABCD"))
        == "✓ RealDebrid (••••)"
    )
    assert settings._token_status(Config(torrentio_base="sort=x|realdebrid=")) == "✗ assente"
    assert settings._token_status(Config(torrentio_base="sort=x")) == "✗ assente"


def test_token_status_other_providers():
    assert (
        settings._token_status(Config(torrentio_base="sort=x|alldebrid=K")) == "✓ AllDebrid (••••)"
    )
    assert settings._token_status(Config(torrentio_base="torbox=K")) == "✓ TorBox (••••)"
    assert settings._token_status(Config(torrentio_base="sort=x|putio=K")) == "✓ Put.io (••••)"


def test_with_token_replaces_only_token():
    base = "sort=qualitysize|realdebrid=OLD"
    out = settings._with_token(base, "NEW")
    assert out == "sort=qualitysize|realdebrid=NEW"
    assert "OLD" not in out


def test_with_token_from_empty():
    assert settings._with_token("", "T") == "sort=qualitysize|realdebrid=T"


def test_with_token_switches_provider():
    # Switching debrid drops the old provider segment, keeps sort, leaves one provider.
    out = settings._with_token("sort=qualitysize|realdebrid=OLD", "K", "alldebrid")
    assert out == "sort=qualitysize|alldebrid=K"
    assert "realdebrid" not in out


def test_items_cover_all_settings():
    cfg = Config(torrentio_base="sort=x|realdebrid=T")
    keys = [it[0] for it in settings._items(cfg)]
    assert keys == [
        "audio_langs",
        "subtitle_langs",
        "auto_play",
        "prefer_cast",
        "cast_device",
        "autoplay",
        "autoplay_lead",
        "hwdec",
        "history_enabled",
        "mpv_quiet",
        "hw_filter",
        "max_resolution",
        "allow_software",
        "allow_dv5",
        "lang_filter",
        "exclude_camrip",
        "min_seeders",
        "dedup",
        "max_streams",
        "torrentio_base",
        "__addons__",
    ]


def test_ask_returns_empty_on_eof(monkeypatch):
    def boom(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert settings._ask("x> ") == ""


def test_mpv_quiet_in_items():
    keys = [it[0] for it in settings._items(Config(torrentio_base="sort=x|realdebrid=T"))]
    assert "mpv_quiet" in keys


def test_items_render_current_values():
    cfg = Config(torrentio_base="sort=x|realdebrid=T", audio_langs=["jpn"], autoplay=False)
    by_key = {it[0]: it for it in settings._items(cfg)}
    assert by_key["audio_langs"][3] == "jpn"
    assert by_key["autoplay"][3] == "off"
    assert by_key["torrentio_base"][3] == "✓ RealDebrid (••••)"


# --- device discovery (Fase 1s) --------------------------------------------


def test_scan_devices_parses_catt_scan(monkeypatch):
    class _P:
        returncode = 0
        stdout = (
            "Scanning Chromecasts...\n"
            "192.168.1.228 - 43PUS9235/12 - Philips TPM191E\n"
            "192.168.1.50 - Soggiorno - Google Nest\n"
            "192.168.1.228 - 43PUS9235/12 - Philips TPM191E\n"  # dup
        )

    monkeypatch.setattr(settings.util, "run_cmd", lambda *a, **k: _P())
    assert settings.scan_devices() == [
        ("43PUS9235/12", "192.168.1.228"),
        ("Soggiorno", "192.168.1.50"),
    ]


def test_scan_devices_empty_on_failure(monkeypatch):
    # run_cmd returns None when catt is missing or the scan fails.
    monkeypatch.setattr(settings.util, "run_cmd", lambda *a, **k: None)
    assert settings.scan_devices() == []


def test_cast_device_item_present():
    keys = [it[0] for it in settings._items(Config(torrentio_base="sort=x|realdebrid=T"))]
    assert "cast_device" in keys
