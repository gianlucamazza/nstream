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
