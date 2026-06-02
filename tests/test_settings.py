"""Unit tests for settings helpers (pure parts; the fzf UI needs a tty)."""

from __future__ import annotations

from nstream import settings
from nstream.config import Config


def test_token_status():
    assert (
        settings._token_status(Config(torrentio_base="sort=x|realdebrid=ABCD"))
        == "✓ impostato (••••)"
    )
    assert settings._token_status(Config(torrentio_base="sort=x|realdebrid=")) == "✗ assente"
    assert settings._token_status(Config(torrentio_base="sort=x")) == "✗ assente"


def test_with_token_replaces_only_token():
    base = "sort=qualitysize|realdebrid=OLD"
    out = settings._with_token(base, "NEW")
    assert out == "sort=qualitysize|realdebrid=NEW"
    assert "OLD" not in out


def test_with_token_from_empty():
    assert settings._with_token("", "T") == "sort=qualitysize|realdebrid=T"


def test_items_cover_all_settings():
    cfg = Config(torrentio_base="sort=x|realdebrid=T")
    keys = [it[0] for it in settings._items(cfg)]
    assert keys == [
        "audio_langs",
        "subtitle_langs",
        "autoplay",
        "autoplay_lead",
        "hwdec",
        "history_enabled",
        "torrentio_base",
        "__addons__",
    ]


def test_items_render_current_values():
    cfg = Config(torrentio_base="sort=x|realdebrid=T", audio_langs=["jpn"], autoplay=False)
    by_key = {it[0]: it for it in settings._items(cfg)}
    assert by_key["audio_langs"][3] == "jpn"
    assert by_key["autoplay"][3] == "off"
    assert by_key["torrentio_base"][3] == "✓ impostato (••••)"
