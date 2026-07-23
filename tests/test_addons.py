"""Unit tests for the Stremio addon client."""

from __future__ import annotations

from typing import cast

from nstream import addons
from nstream.config import Config

CFG = Config(torrentio_base="sort=qualitysize|realdebrid=TOK")


def test_base_of():
    assert addons._base_of("https://x/y/manifest.json") == "https://x/y"
    assert addons._base_of("https://x/y/") == "https://x/y"


def test_parse_manifest_string_and_object_resources():
    data = {
        "name": "Demo",
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "resources": [
            "catalog",
            {"name": "stream", "types": ["movie"], "idPrefixes": ["tt", "kitsu"]},
        ],
        "catalogs": [{"type": "movie", "id": "top"}],
    }
    a = addons._parse_manifest("https://d/manifest.json", data)
    assert a.base == "https://d"
    assert a.resources["catalog"] == {"types": ["movie", "series"], "idPrefixes": ["tt"]}
    assert a.resources["stream"] == {"types": ["movie"], "idPrefixes": ["tt", "kitsu"]}
    assert a.catalogs == (("movie", "top", "top"),)


def test_serves_type_and_idprefix():
    a = addons.Addon(
        base="b", name="n",
        resources={"stream": {"types": ["movie"], "idPrefixes": ["tt"]}},
    )  # fmt: skip
    assert addons.serves(a, "stream", "movie", "tt1") is True
    assert addons.serves(a, "stream", "series", "tt1") is False  # wrong type
    assert addons.serves(a, "stream", "movie", "kitsu9") is False  # wrong idPrefix
    assert addons.serves(a, "subtitles", "movie", "tt1") is False  # missing resource


def test_serves_empty_idprefixes_allows_any():
    a = addons.Addon(base="b", name="n", resources={"subtitles": {"types": [], "idPrefixes": []}})
    assert addons.serves(a, "subtitles", "movie", "anything") is True


def test_effective_addons_builtins(monkeypatch):
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: None)  # extras unreachable
    eff = addons.effective_addons(CFG)
    names = [a.name for a in eff]
    assert names == ["Cinemeta", "Torrentio", "OpenSubtitles"]
    torrentio = next(a for a in eff if a.name == "Torrentio")
    assert torrentio.base.startswith("https://torrentio.strem.fun/")
    assert "stream" in torrentio.resources


def test_effective_addons_includes_extras(monkeypatch):
    extra = addons.Addon(
        base="https://x", name="Extra", resources={"stream": {"types": [], "idPrefixes": []}}
    )
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: extra)
    cfg = Config(torrentio_base="tb", addons=["https://x/manifest.json"])
    assert "Extra" in [a.name for a in addons.effective_addons(cfg)]


def test_load_addon_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    calls = {"n": 0}

    def fake_get(url, **k):
        calls["n"] += 1
        return {"name": "Cached", "types": ["movie"], "resources": ["stream"]}

    monkeypatch.setattr(addons.net, "http_get_json", fake_get)
    a1 = addons.load_addon("https://c/manifest.json")
    a2 = addons.load_addon("https://c/manifest.json")  # served from cache
    assert a1 is not None and a2 is not None
    assert a2.name == "Cached"
    assert calls["n"] == 1  # fetched once


def test_load_addon_unreachable_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    def boom(url, **k):
        raise addons.net.NetworkError("down")

    monkeypatch.setattr(addons.net, "http_get_json", boom)
    assert addons.load_addon("https://c/manifest.json") is None


def test_parse_manifest_guards_non_dict():
    # A corrupt/partial cache payload must not crash the whole flow.
    a = addons._parse_manifest("https://d/manifest.json", cast("dict", "not a dict"))
    assert a.resources == {}
    assert a.base == "https://d"


def test_load_addon_uses_single_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    seen = {}

    def fake_get(url, *, what="", retries=3):
        seen["retries"] = retries
        return {"name": "X", "resources": ["stream"]}

    monkeypatch.setattr(addons.net, "http_get_json", fake_get)
    addons.load_addon("https://x/manifest.json", use_cache=False)
    assert seen["retries"] == 1


def test_cache_file_has_no_token(tmp_path, monkeypatch):
    """The manifest cache must be keyed by hash, never store the token-bearing URL."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(
        addons.net, "http_get_json", lambda url, **k: {"name": "X", "resources": ["stream"]}
    )
    addons.load_addon("https://torrentio.strem.fun/realdebrid=SECRETTOK/manifest.json")
    cache_text = (tmp_path / "nstream" / "manifests.json").read_text()
    assert "SECRETTOK" not in cache_text
