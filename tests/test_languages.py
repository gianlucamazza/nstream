"""Unit tests for the language registry (single source of truth)."""

from __future__ import annotations

from nstream import languages


def test_codes_unique():
    codes = [lang.code for lang in languages.LANGUAGES]
    assert len(codes) == len(set(codes))


def test_name_lookup_and_fallback():
    assert languages.name("ita") == "Italiano"
    assert languages.name("jpn") == "日本語"
    assert languages.name("xyz") == "XYZ"  # unknown → upper-cased code


def test_selectable_excludes_multi():
    sel = {lang.code for lang in languages.selectable()}
    assert "multi" not in sel
    assert {"ita", "eng", "jpn"} <= sel


def test_registry_covers_historical_languages():
    # The eight languages that predate the registry must still be present (regression).
    for code in ("ita", "eng", "fra", "spa", "deu", "rus", "por", "multi"):
        assert code in languages.by_code


def test_normalize_aliases_tokens_and_names():
    assert languages.normalize("it") == "ita"  # ISO-639-1
    assert languages.normalize("EN") == "eng"  # case-insensitive
    assert languages.normalize("eng") == "eng"  # canonical
    assert languages.normalize("italian") == "ita"  # token
    assert languages.normalize("Español") == "spa"  # native name
    assert languages.normalize("ger") == "deu"  # 639-2/B alias
    assert languages.normalize(" fr ") == "fra"  # trimmed
    assert languages.normalize("xx") is None
    assert languages.normalize("") is None


def test_lat_token_maps_to_spanish():
    assert languages.normalize("LAT") == "spa"  # Latino releases
