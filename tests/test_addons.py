"""Unit tests for the Stremio addon client."""

from __future__ import annotations

from typing import cast

import pytest

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


def test_effective_addons_torrentio_disabled(monkeypatch):
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: None)
    cfg = Config(torrentio_base="tb", torrentio_enabled=False)
    names = [a.name for a in addons.effective_addons(cfg)]
    assert names == ["Cinemeta", "OpenSubtitles"]
    assert "Torrentio" not in names


def test_has_stream_source(monkeypatch):
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: None)
    assert addons.has_stream_source(Config(torrentio_base="tb")) is True
    assert addons.has_stream_source(Config(torrentio_base="tb", torrentio_enabled=False)) is False
    extra = addons.Addon(
        base="https://x", name="Comet", resources={"stream": {"types": [], "idPrefixes": []}}
    )
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: extra)
    cfg = Config(torrentio_base="tb", torrentio_enabled=False, addons=["https://x/manifest.json"])
    assert addons.has_stream_source(cfg) is True


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


def test_parse_manifest_search_catalogs():
    data = {
        "resources": ["catalog"],
        "types": ["anime", "movie"],
        "catalogs": [
            {"type": "anime", "id": "kitsu-anime-list", "extra": [{"name": "search"}]},
            {"type": "movie", "id": "trending", "extra": [{"name": "genre"}]},
            {"type": "movie", "id": "legacy", "extraSupported": ["search"]},
        ],
    }
    a = addons._parse_manifest("https://k/manifest.json", data)
    assert a.search_catalogs == (("anime", "kitsu-anime-list"), ("movie", "legacy"))
    assert addons.search_catalog(a, "movie") == "legacy"
    assert addons.search_catalog(a, "series") is None
    # optional extras stay board-browsable; required search is dropped (next test)
    assert ("movie", "trending", "trending") in a.board_catalogs


def test_parse_manifest_skips_required_search_from_board():
    data = {
        "name": "TMDB",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "catalogs": [
            {"type": "movie", "id": "tmdb.top", "name": "Popular", "extra": [{"name": "skip"}]},
            {
                "type": "movie",
                "id": "tmdb.search",
                "name": "Search",
                "extra": [{"name": "search", "isRequired": True}],
            },
            {
                "type": "anime",
                "id": "kitsu-anime-list",
                "name": "Kitsu",
                "extraRequired": ["search"],
            },
        ],
    }
    a = addons._parse_manifest("https://t/manifest.json", data)
    assert a.board_catalogs == (("movie", "tmdb.top", "Popular"),)
    assert ("movie", "tmdb.search", "Search") in a.catalogs
    assert ("anime", "kitsu-anime-list", "Kitsu") in a.catalogs


def test_parse_manifest_required_genre_is_board_ok():
    """Required genre (and skip) stay on the board; required search still does not (ADR 0048)."""
    data = {
        "name": "TMDB",
        "resources": ["catalog"],
        "types": ["movie"],
        "catalogs": [
            {
                "type": "movie",
                "id": "tmdb.genres",
                "name": "Genres",
                "extra": [
                    {
                        "name": "genre",
                        "isRequired": True,
                        "options": ["Action", "Comedy"],
                    },
                    {"name": "skip"},
                ],
            },
            {
                "type": "movie",
                "id": "tmdb.search",
                "name": "Search",
                "extra": [{"name": "search", "isRequired": True}],
            },
        ],
    }
    a = addons._parse_manifest("https://t/manifest.json", data)
    assert a.board_catalogs == (("movie", "tmdb.genres", "Genres"),)
    info = next(i for t, c, i in a.catalog_extra if c == "tmdb.genres")
    assert info.supports_genre and info.supports_skip and info.genre_required
    assert info.genres == ("Action", "Comedy")


def test_catalog_fetch_type_prefers_section_then_declared():
    a = addons.Addon(
        base="https://k",
        name="Kitsu",
        resources={"catalog": {"types": ["anime", "movie"], "idPrefixes": ["kitsu"]}},
        catalogs=(
            ("anime", "kitsu-anime-trending", "Kitsu Trending"),
            ("movie", "tmdb.top", "Popular"),
        ),
    )
    assert addons.catalog_fetch_type(a, "movie", "tmdb.top") == "movie"
    assert addons.catalog_fetch_type(a, "movie", "kitsu-anime-trending") == "anime"
    assert addons.catalog_fetch_type(a, "series", "missing") is None


def test_extra_catalogs_from_unlocked_manifests(monkeypatch):
    """Board rows come from cfg.addons manifests — not a marketplace, not Cinemeta pins."""
    tmdb = addons._parse_manifest(
        "https://tmdb/manifest.json",
        {
            "name": "The Movie Database Addon",
            "resources": ["catalog"],
            "types": ["movie", "series"],
            "catalogs": [
                {"type": "movie", "id": "tmdb.top", "name": "Popular", "extra": [{"name": "skip"}]},
                {
                    "type": "series",
                    "id": "tmdb.top",
                    "name": "Popular",
                    "extra": [{"name": "skip"}],
                },
                {
                    "type": "movie",
                    "id": "tmdb.search",
                    "name": "Search",
                    "extra": [{"name": "search", "isRequired": True}],
                },
            ],
        },
    )
    kitsu = addons._parse_manifest(
        "https://kitsu/manifest.json",
        {
            "name": "Anime Kitsu",
            "resources": ["catalog"],
            "types": ["anime", "movie", "series"],
            "catalogs": [
                {"type": "anime", "id": "kitsu-anime-trending", "name": "Kitsu Trending"},
                {
                    "type": "anime",
                    "id": "kitsu-anime-list",
                    "name": "Kitsu",
                    "extra": [{"name": "search", "isRequired": True}],
                },
            ],
        },
    )
    by_url = {
        "https://tmdb/manifest.json": tmdb,
        "https://kitsu/manifest.json": kitsu,
    }
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: by_url.get(url))
    cfg = Config(
        torrentio_base="tb",
        addons=["https://tmdb/manifest.json", "https://kitsu/manifest.json"],
    )
    movie = addons.extra_catalogs(cfg, "movie")
    series = addons.extra_catalogs(cfg, "series")
    assert movie == [
        ("tmdb.top", "The Movie Database Addon · Popular"),
        ("kitsu-anime-trending", "Anime Kitsu · Kitsu Trending"),
    ]
    assert series == [
        ("tmdb.top", "The Movie Database Addon · Popular"),
        ("kitsu-anime-trending", "Anime Kitsu · Kitsu Trending"),
    ]
    assert "tmdb.search" not in {c for c, _ in movie}
    assert "kitsu-anime-list" not in {c for c, _ in movie}


def test_catalog_extra_info_from_unlocked_manifest(monkeypatch):
    """Lookup follows extra_catalogs (first id wins); Cinemeta pins stay None."""
    tmdb = addons._parse_manifest(
        "https://tmdb/manifest.json",
        {
            "name": "The Movie Database Addon",
            "resources": ["catalog"],
            "types": ["movie", "series"],
            "catalogs": [
                {
                    "type": "movie",
                    "id": "tmdb.top",
                    "name": "Popular",
                    "extra": [
                        {"name": "genre", "options": ["Action", "Drama"]},
                        {"name": "skip"},
                    ],
                },
                {
                    "type": "movie",
                    "id": "tmdb.genres",
                    "name": "Genres",
                    "extra": [{"name": "genre", "isRequired": True, "options": ["Horror"]}],
                },
            ],
        },
    )
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: tmdb)
    cfg = Config(torrentio_base="tb", addons=["https://tmdb/manifest.json"])
    top = addons.catalog_extra_info(cfg, "movie", "tmdb.top")
    assert top is not None and top.supports_genre and top.supports_skip
    assert top.genres == ("Action", "Drama") and not top.genre_required
    req = addons.catalog_extra_info(cfg, "movie", "tmdb.genres")
    assert req is not None and req.genre_required and req.genres == ("Horror",)
    assert addons.catalog_extra_info(cfg, "movie", "top") is None
    assert addons.catalog_extra_info(cfg, None, "tmdb.top") is None
    movie = addons.extra_catalogs(cfg, "movie")
    assert ("tmdb.genres", "The Movie Database Addon · Genres") in movie


def test_extra_catalogs_skips_builtin_ids(monkeypatch):
    extra = addons.Addon(
        base="https://x",
        name="Clone",
        resources={"catalog": {"types": ["movie"], "idPrefixes": ["tt"]}},
        catalogs=(("movie", "top", "Popolari"), ("movie", "mine", "Mine")),
        board_catalogs=(("movie", "top", "Popolari"), ("movie", "mine", "Mine")),
    )
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: extra)
    cfg = Config(torrentio_base="tb", addons=["https://x/manifest.json"])
    assert addons.extra_catalogs(cfg, "movie") == [("mine", "Clone · Mine")]


def test_cinemeta_builtin_is_searchable(monkeypatch):
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: None)
    cine = addons.effective_addons(CFG)[0]
    assert addons.search_catalog(cine, "movie") == "top"
    assert addons.search_catalog(cine, "series") == "top"


def test_load_addon_negative_caches_a_failed_fetch(monkeypatch):
    calls = []

    def down(url, **k):
        calls.append(url)
        raise addons.net.NetworkError("giù")

    monkeypatch.setattr(addons.net, "http_get_json", down)
    monkeypatch.setattr(addons, "_failed_at", {})
    url = "https://down.example/manifest.json"
    assert addons.load_addon(url) is None
    assert addons.load_addon(url) is None
    assert len(calls) == 1  # second call served by the negative cache


def test_stale_manifest_served_without_blocking(monkeypatch):
    # P2: a stale manifest used to be refreshed synchronously on the critical path.
    url = "https://slow.example/manifest.json"
    key = addons.hashlib.sha256(url.encode()).hexdigest()
    old = {"ts": 0, "manifest": {"name": "Slow", "resources": ["stream"], "types": ["movie"]}}
    monkeypatch.setattr(addons, "_load_cache", lambda: {key: dict(old)})
    refreshed = []
    monkeypatch.setattr(addons, "_refresh_in_background", lambda u, k: refreshed.append(u))
    monkeypatch.setattr(
        addons.net, "http_get_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sync"))
    )
    a = addons.load_addon(url)
    assert a is not None and a.name == "Slow" and refreshed == [url]


def test_recent_refresh_failure_suppresses_retry(monkeypatch):
    url = "https://down.example/manifest.json"
    key = addons.hashlib.sha256(url.encode()).hexdigest()
    entry = {"ts": 0, "fail_ts": int(addons.time.time()), "manifest": {"name": "Down"}}
    monkeypatch.setattr(addons, "_load_cache", lambda: {key: entry})
    monkeypatch.setattr(addons, "_refresh_in_background", lambda u, k: pytest.fail("retry"))
    assert addons.load_addon(url) is not None


# --- ADR 0049 Trakt catalog slice (not an indexer) -------------------------


_TRAKT_TV_MANIFEST = {
    "id": "community.trakt-tv",
    "name": "Trakt Tv",
    "resources": [{"name": "meta", "types": ["series", "movie"], "idPrefixes": ["trakt:"]}],
    "types": [],
    "catalogs": [
        {
            "type": "trakt",
            "id": "trakt_popular_movies",
            "name": "trakt - Popular movies",
            "extra": [
                {"name": "genre", "isRequired": False, "options": ["action", "drama"]},
                {"name": "skip", "isRequired": False},
            ],
        },
        {
            "type": "trakt",
            "id": "trakt_trending_series",
            "name": "trakt - Trending series",
            "extra": [{"name": "skip", "isRequired": False}],
        },
        {
            "type": "trakt",
            "id": "trakt_watchlist",
            "name": "trakt - Watchlist",
        },
        {
            "type": "trakt",
            "id": "trakt_search_movies",
            "name": "trakt - search movies",
            "extra": [{"name": "search", "isRequired": True}],
        },
    ],
}


def _trakt_addon(url: str = "https://trakt.example/abc/manifest.json") -> addons.Addon:
    return addons._parse_manifest(url, _TRAKT_TV_MANIFEST)


def test_is_trakt_catalog_addon_shipping_shape():
    trakt = _trakt_addon()
    assert addons.is_trakt_catalog_addon(trakt)
    assert not addons.serves(trakt, "catalog", "trakt")
    assert not addons.serves(trakt, "stream", "movie")
    assert addons.can_fetch_catalog(trakt, "trakt")
    tmdb = addons._parse_manifest(
        "https://tmdb/manifest.json",
        {
            "name": "The Movie Database Addon",
            "resources": ["catalog"],
            "catalogs": [{"type": "movie", "id": "tmdb.top", "name": "Popular"}],
        },
    )
    assert not addons.is_trakt_catalog_addon(tmdb)
    assert addons.can_fetch_catalog(tmdb, "movie")
    assert not addons.is_trakt_catalog_addon(
        addons.Addon(base="https://c", name="Cinemeta", resources={}, builtin=True)
    )


def test_trakt_board_section_from_id_or_type():
    assert addons.trakt_board_section("trakt", "trakt_popular_movies", "Popular movies") == "movie"
    assert addons.trakt_board_section("trakt", "trakt_trending_series", "Trending series") == "series"
    assert addons.trakt_board_section("movie", "watchlist", "Watchlist") == "movie"
    assert addons.trakt_board_section("trakt", "trakt_watchlist", "Watchlist") is None


def test_trakt_catalogs_on_board_not_in_extra_catalogs(monkeypatch):
    """Type `trakt` + no catalog resource still lists; search stays off (ADR 0048/0049)."""
    trakt = _trakt_addon()
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: trakt)
    cfg = Config(torrentio_base="tb", trakt_addon=trakt.manifest_url)
    movie = addons.trakt_catalogs(cfg, "movie")
    series = addons.trakt_catalogs(cfg, "series")
    movie_ids = {c for c, _ in movie}
    series_ids = {c for c, _ in series}
    assert movie_ids == {"trakt_popular_movies", "trakt_watchlist"}
    assert series_ids == {"trakt_trending_series", "trakt_watchlist"}
    assert "trakt_search_movies" not in movie_ids
    assert addons.extra_catalogs(cfg, "movie") == []
    assert addons.extra_catalogs(cfg, "series") == []
    info = addons.catalog_extra_info(cfg, "movie", "trakt_popular_movies")
    assert info is not None and info.supports_genre and info.supports_skip
    assert info.genres == ("action", "drama")


def test_effective_addons_includes_trakt_addon_once(monkeypatch):
    trakt = _trakt_addon()
    seen: list[str] = []

    def load(url, **k):
        seen.append(url)
        return trakt if "trakt" in url else None

    monkeypatch.setattr(addons, "load_addon", load)
    url = trakt.manifest_url
    cfg = Config(torrentio_base="tb", addons=[url], trakt_addon=url)
    names = [a.name for a in addons.effective_addons(cfg) if not a.builtin]
    assert names.count("Trakt Tv") == 1
    assert seen.count(url) == 1


def test_trakt_addon_from_cfg_addons_still_grouped(monkeypatch):
    """A Fonti paste into cfg.addons (no trakt_addon key) is still a Trakt catalog."""
    trakt = _trakt_addon()
    monkeypatch.setattr(addons, "load_addon", lambda url, **k: trakt)
    cfg = Config(torrentio_base="tb", addons=[trakt.manifest_url])
    assert ("trakt_popular_movies", "trakt - Popular movies") in addons.trakt_catalogs(cfg, "movie")
    assert addons.extra_catalogs(cfg, "movie") == []
