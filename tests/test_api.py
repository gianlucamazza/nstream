"""Unit tests for api resource dispatch and aggregation across addons."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from dataclasses import replace
from email.message import Message
from typing import cast

import pytest

from nstream import addons, api
from nstream.config import Config
from nstream.types import Stream

CFG = Config(torrentio_base="tb")


def test_single_source_deadline_ignores_late_success(monkeypatch):
    from nstream.state import breaker

    release = threading.Event()
    ended = threading.Event()
    records = []
    monkeypatch.setattr(api, "_GATHER_BUDGET", 0.02)
    monkeypatch.setattr(breaker, "record_failure", lambda *a, **kw: records.append("failed"))
    monkeypatch.setattr(breaker, "record_success", lambda *a: records.append("success"))

    def slow():
        try:
            release.wait(1)
            return [1]
        finally:
            ended.set()

    try:
        assert api._gather([slow], keys=["http://test"]) == []
        assert records == ["failed"]
    finally:
        release.set()
    assert ended.wait(1)
    assert records == ["failed"]


@pytest.fixture(autouse=True)
def _clear_meta_cache():
    api.clear_cache()
    yield
    api.clear_cache()


def _addon(name, base, resource, types=("movie",), idp=("tt",), catalogs=(), search=None):
    if search is None:  # a catalog addon searchable on its `top` catalog, like Cinemeta
        search = tuple((t, "top") for t in types) if resource == "catalog" else ()
    return addons.Addon(
        base=base,
        name=name,
        resources={resource: {"types": list(types), "idPrefixes": list(idp)}},
        catalogs=catalogs,
        search_catalogs=search,
    )


def test_streams_aggregate_and_dedup(monkeypatch):
    a = _addon("A", "http://a", "stream")
    b = _addon("B", "http://b", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])

    def fake_get(url, **k):
        if url.startswith("http://a"):
            return {"streams": [{"url": "http://u1"}, {"url": "http://u2"}]}
        return {"streams": [{"url": "http://u2"}, {"url": "http://u3"}]}  # u2 duplicate

    monkeypatch.setattr(api, "http_get_json", fake_get)
    assert [s["url"] for s in api.streams(CFG, "movie", "tt1")] == [
        "http://u1",
        "http://u2",
        "http://u3",
    ]


def test_streams_one_addon_fails_does_not_block(monkeypatch, capsys):
    a = _addon("A", "http://a", "stream")
    b = _addon("B", "http://b", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])

    def fake_get(url, **k):
        if url.startswith("http://a"):
            raise api.NetworkError("down")
        return {"streams": [{"url": "http://u3"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    assert [s["url"] for s in api.streams(CFG, "movie", "tt1")] == ["http://u3"]
    # No per-addon error spam (avoids the double "stream … / nessuno stream" message).
    assert capsys.readouterr().err == ""


def test_streams_stuck_addon_dropped_at_deadline(monkeypatch):
    """One hung addon must not hold the gather hostage: past the shared _GATHER_BUDGET
    its future is dropped (empty result, no crash) and the fast addon still answers."""
    a = _addon("A", "http://a", "stream")
    b = _addon("B", "http://b", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])
    monkeypatch.setattr(api, "_GATHER_BUDGET", 0.1)
    release = threading.Event()

    def fake_get(url, **k):
        if url.startswith("http://a"):
            release.wait(5.0)  # hung addon: never answers within the budget
            return {"streams": [{"url": "http://late"}]}
        return {"streams": [{"url": "http://fast"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    try:
        assert [s["url"] for s in api.streams(CFG, "movie", "tt1")] == ["http://fast"]
    finally:
        release.set()  # unblock the abandoned worker so the suite exits promptly


def test_gather_progress_on_tty(monkeypatch):
    """Multi-source gather on a TTY surfaces progressive fonti lines (not one frozen wait)."""
    from nstream import ui as ui_mod

    calls: list[str] = []

    class _Stderr:
        def isatty(self):
            return True

    monkeypatch.setattr(api.sys, "stderr", _Stderr())
    # _gather imports ui lazily; patch the real ui module it binds.
    monkeypatch.setattr(ui_mod, "status", lambda msg, *, kind="info": calls.append(f"status:{msg}"))
    monkeypatch.setattr(ui_mod, "progress", lambda msg: calls.append(f"progress:{msg}"))
    monkeypatch.setattr(ui_mod, "progress_done", lambda msg="": calls.append(f"done:{msg}"))

    tasks = [lambda: [{"x": 1}], lambda: [{"y": 2}]]
    out = api._gather(tasks, labels=["Alpha", "Beta"])
    assert len(out) == 2
    assert any(c.startswith("status:interrogo 2") for c in calls)
    assert any("fonti 1/2" in c and "Alpha" in c for c in calls)
    assert any("fonti 2/2" in c and "Beta" in c for c in calls)
    assert any(c.startswith("done:fonti:") for c in calls)


def test_streams_pass_addon_labels_to_gather(monkeypatch):
    """streams() wires addon names + bases into _gather (progress + breaker keys)."""
    a = _addon("Comet", "http://c", "stream")
    b = _addon("MediaFusion", "http://m", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])
    monkeypatch.setattr(api, "http_get_json", lambda url, **k: {"streams": [{"url": url[-8:]}]})
    seen = {}

    def capture(tasks, *, labels=None, keys=None):
        seen["labels"] = list(labels or [])
        seen["keys"] = list(keys or [])
        return [{"url": "http://u1"}]

    monkeypatch.setattr(api, "_gather", capture)
    api.streams(CFG, "movie", "tt1")
    assert seen["labels"] == ["Comet", "MediaFusion"]
    assert seen["keys"] == ["http://c", "http://m"]


def test_gather_skips_open_breaker(monkeypatch, tmp_path):
    """Open breaker → no network for that key; healthy sources still return."""
    from nstream.state import breaker as brk

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    brk.record_failure("http://dead")
    brk.record_failure("http://dead")
    brk.record_failure("http://dead")  # → Open
    assert brk.allow("http://dead") is False

    called: list[str] = []

    def dead():
        called.append("dead")
        return [{"url": "nope"}]

    def live():
        called.append("live")
        return [{"url": "ok"}]

    out = api._gather(
        [dead, live],
        labels=["Dead", "Live"],
        keys=["http://dead", "http://live"],
    )
    assert called == ["live"]
    assert [s["url"] for s in out] == ["ok"]


def test_gather_records_network_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from nstream.state import breaker as brk

    def boom():
        raise api.NetworkError("timeout")

    out = api._gather([boom], labels=["X"], keys=["http://x"])
    assert out == []
    rec = brk._read().get("http://x")
    assert rec and rec.get("fails") == 1


def test_streams_skips_addon_not_serving_type(monkeypatch):
    a = _addon("A", "http://a", "stream", types=("series",))  # only series
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a])
    monkeypatch.setattr(api, "http_get_json", lambda *x, **k: pytest.fail("should not be called"))
    assert api.streams(CFG, "movie", "tt1") == []


def test_subtitles_aggregate(monkeypatch):
    a = _addon("A", "http://a", "subtitles", idp=())
    b = _addon("B", "http://b", "subtitles", idp=())
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])

    def fake_get(url, **k):
        n = "a" if url.startswith("http://a") else "b"
        return {"subtitles": [{"id": f"{n}1", "url": f"{n}.srt"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    assert {s["url"] for s in api.subtitles(CFG, "movie", "tt1")} == {"a.srt", "b.srt"}


def test_subtitles_video_hash_query_tags_and_wins_dedup(monkeypatch):
    """With a videoHash the addon is queried twice (hash extra + plain). Only entries the
    addon marks `m == "h"` (MOVIEHASH match) are tagged: on an unknown hash the addon
    falls back to the full imdb set (`m == "i"`) and tagging those would fabricate the sync
    guarantee. The tagged copy, fetched first, survives the by-url dedup (ADR 0018)."""
    a = _addon("A", "http://a", "subtitles", idp=())
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a])
    urls: list[str] = []

    def fake_get(url, **k):
        urls.append(url)
        if "videoHash=" in url:
            return {
                "subtitles": [
                    {"id": "s1", "url": "same.srt", "m": "h"},
                    {"id": "s3", "url": "fallback.srt", "m": "i"},  # imdb fallback: no tag
                ]
            }
        return {"subtitles": [{"id": "s1", "url": "same.srt"}, {"id": "s2", "url": "other.srt"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    out = api.subtitles(
        CFG, "movie", "tt1", video_hash="ab" * 8, video_size=1000, filename="V x.mkv"
    )
    hash_urls = [u for u in urls if "videoHash=" in u]
    assert len(hash_urls) == 1 and len(urls) == 2
    assert "videoSize=1000" in hash_urls[0] and "filename=V%20x.mkv" in hash_urls[0]
    by_url = {s["url"]: s for s in out}
    assert by_url["same.srt"].get("hash_match") is True  # m=="h" → tagged, survived dedup
    assert by_url["fallback.srt"].get("hash_match") is None  # m=="i" → NOT a hash match
    assert by_url["other.srt"].get("hash_match") is None


def test_subtitles_without_hash_single_query(monkeypatch):
    a = _addon("A", "http://a", "subtitles", idp=())
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a])
    urls: list[str] = []
    monkeypatch.setattr(
        api, "http_get_json", lambda url, **k: urls.append(url) or {"subtitles": []}
    )
    api.subtitles(CFG, "movie", "tt1")
    assert len(urls) == 1 and "videoHash" not in urls[0]


def test_catalog_extra_must_declare_catalog(monkeypatch):
    builtin = addons.Addon(
        base="http://cine", name="Cinemeta", builtin=True,
        resources={"catalog": {"types": ["movie"], "idPrefixes": ["tt"]}},
    )  # fmt: skip
    extra_no = _addon("X", "http://x", "catalog", catalogs=())  # declares no catalogs
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [builtin, extra_no])
    seen = []

    def fake_get(url, **k):
        seen.append(url)
        return {"metas": [{"id": "tt1", "name": "M"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    api.catalog(CFG, "movie", "top")
    # only the built-in is queried; the extra didn't declare (movie, top)
    assert all("http://cine" in u for u in seen)


def test_episodes_returns_first_with_videos(monkeypatch):
    a = _addon("A", "http://a", "meta", types=("series",))
    b = _addon("B", "http://b", "meta", types=("series",))
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])

    def fake_get(url, **k):
        if url.startswith("http://a"):
            return {"meta": {"videos": []}}  # no usable episodes
        return {"meta": {"videos": [{"season": 1, "episode": 2}, {"season": 1, "episode": 1}]}}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    vids = api.episodes(CFG, "tt1")
    assert [(v["season"], v["episode"]) for v in vids] == [(1, 1), (1, 2)]  # sorted


def test_meta_cached_disk_persists_and_hits(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(api, "meta", lambda cfg, typ, vid: calls.append(vid) or {"name": "X"})
    first = api.meta_cached_disk(CFG, "movie", "tt9")
    second = api.meta_cached_disk(CFG, "movie", "tt9")
    assert first == second == {"name": "X"}
    assert calls == ["tt9"]  # second call served from disk, no re-fetch


def test_meta_cached_disk_empty_not_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(api, "meta", lambda cfg, typ, vid: calls.append(vid) or {})
    api.meta_cached_disk(CFG, "movie", "tt9")
    api.meta_cached_disk(CFG, "movie", "tt9")
    assert calls == ["tt9", "tt9"]  # empty result isn't persisted → re-fetched


# --- expected runtime (ADR 0028) -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("136 min", 8160.0),
        ("55 min", 3300.0),
        ("1h 30min", 5400.0),
        ("2 h", 7200.0),
        ("", 0.0),
        ("n/a", 0.0),
    ],
)
def test_parse_runtime_s_formats(raw, seconds):
    assert api.parse_runtime_s(raw) == seconds


def test_expected_runtime_series_reads_series_meta(monkeypatch):
    # Cinemeta puts `runtime` on the SERIES meta (typical episode length); the episode
    # entries carry none, so the series id must be derived from the episode id.
    seen = []
    monkeypatch.setattr(
        api,
        "meta_cached_disk",
        lambda cfg, typ, vid: seen.append((typ, vid)) or {"runtime": "55 min"},
    )
    assert api.expected_runtime_s(CFG, "series", "tt5675620:1:1") == 3300.0
    assert seen == [("series", "tt5675620")]


def test_expected_runtime_movie_reads_own_meta(monkeypatch):
    seen = []
    monkeypatch.setattr(
        api,
        "meta_cached_disk",
        lambda cfg, typ, vid: seen.append((typ, vid)) or {"runtime": "136 min"},
    )
    assert api.expected_runtime_s(CFG, "movie", "tt0330793") == 8160.0
    assert seen == [("movie", "tt0330793")]


def test_expected_runtime_zero_when_meta_missing(monkeypatch):
    monkeypatch.setattr(api, "meta_cached_disk", lambda cfg, typ, vid: {})
    assert api.expected_runtime_s(CFG, "series", "tt1:1:1") == 0.0
    monkeypatch.setattr(api, "meta_cached_disk", lambda cfg, typ, vid: {"runtime": None})
    assert api.expected_runtime_s(CFG, "movie", "tt1") == 0.0


def test_cat_map_browse_keywords():
    # --browse keyword → Cinemeta catalog id (consumed by api.catalog/browse).
    assert api.CAT_MAP == {"popolari": "top", "nuovi": "year", "top": "imdbRating"}


# --- gzip, browse, cache (Fase 1p) -----------------------------------------

import gzip as _gzip  # noqa: E402
import json as _json  # noqa: E402


class _Resp:
    """Minimal urlopen() context-manager stand-in."""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self, size=-1):
        body, self._body = self._body[:size], self._body[size:]
        return body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_get_json_plain(monkeypatch):
    body = _json.dumps({"ok": 1}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    assert api.http_get_json("http://x", what="t") == {"ok": 1}


def test_http_get_json_gzip(monkeypatch):
    body = _gzip.compress(_json.dumps({"ok": 2}).encode())
    resp = _Resp(body, {"Content-Encoding": "gzip"})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp)
    assert api.http_get_json("http://x", what="t") == {"ok": 2}


def test_http_get_json_corrupt_gzip_raises(monkeypatch):
    resp = _Resp(b"\x1f\x8bnot-gzip", {"Content-Encoding": "gzip"})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp)
    with pytest.raises(api.NetworkError, match="non valida"):
        api.http_get_json("http://x", what="t")


def test_browse_combines_movie_and_series(monkeypatch):
    cine = _addon(
        "Cine",
        "http://cine",
        "catalog",
        types=("movie", "series"),
        catalogs=(("movie", "top"), ("series", "top")),
    )
    cine = replace(cine, builtin=True)
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [cine])

    def fake_get(url, **k):
        typ = "movie" if "/movie/" in url else "series"
        return {"metas": [{"id": f"{typ}1", "name": typ}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    ids = [m["id"] for m in api.browse(CFG, "top")]
    assert set(ids) == {"movie1", "series1"}


def test_search_typed_fetches_only_that_type(monkeypatch):
    cine = _addon("Cine", "http://cine", "catalog", types=("movie", "series"))
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [cine])
    seen = []

    def fake_get(url, **k):
        seen.append(url)
        typ = "movie" if "/movie/" in url else "series"
        return {"metas": [{"id": f"{typ}1", "name": typ}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    ids = [m["id"] for m in api.search(CFG, "dune", typ="series")]
    assert ids == ["series1"]
    assert all("/series/" in u for u in seen)


def test_search_untyped_fetches_both_types(monkeypatch):
    cine = _addon("Cine", "http://cine", "catalog", types=("movie", "series"))
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [cine])

    def fake_get(url, **k):
        typ = "movie" if "/movie/" in url else "series"
        return {"metas": [{"id": f"{typ}1", "name": typ}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    ids = [m["id"] for m in api.search(CFG, "dune")]
    assert set(ids) == {"movie1", "series1"}


def test_search_ranks_exact_and_accent_insensitive_match(monkeypatch):
    cine = _addon("Cine", "http://cine", "catalog", types=("movie",))
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [cine])

    def fake_get(url, **k):
        return {
            "metas": [
                {"id": "tt2", "name": "The Matrix Reloaded"},
                {"id": "tt1", "name": "Matrix"},
                {"id": "tt3", "name": "Màtrix"},
            ]
        }

    monkeypatch.setattr(api, "http_get_json", fake_get)
    assert [m["id"] for m in api.search(CFG, "Matrix", typ="movie")] == ["tt1", "tt3", "tt2"]


def test_search_ties_keep_addon_order(monkeypatch):
    cine = _addon("Cine", "http://cine", "catalog", types=("movie",))
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [cine])

    def fake_get(url, **k):
        return {
            "metas": [
                {"id": "tt3", "name": "Zeta"},
                {"id": "tt2", "name": "Spider Max"},
                {"id": "tt1", "name": "Spider-Man"},
                {"id": "tt4", "name": "Alpha"},
            ]
        }

    monkeypatch.setattr(api, "http_get_json", fake_get)
    assert [m["id"] for m in api.search(CFG, "Spider", typ="movie")] == ["tt2", "tt1", "tt3", "tt4"]


def test_search_skips_addons_without_a_searchable_catalog(monkeypatch):
    # Regression (2026-10-01): anime-kitsu serves `catalog` for movie but only declares
    # search on an anime catalog; querying `movie/top/search=` returned fuzzy anime rows.
    cine = _addon("Cine", "http://cine", "catalog", types=("movie",))
    kitsu = _addon("Kitsu", "http://kitsu", "catalog", types=("anime", "movie"),
                   search=(("anime", "kitsu-anime-list"),))  # fmt: skip
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [cine, kitsu])
    seen = []

    def fake_get(url, **k):
        seen.append(url)
        return {"metas": [{"id": "tt1343092", "name": "The Great Gatsby"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    api.search(CFG, "Il grande Gatsby", typ="movie")
    assert seen == ["http://cine/catalog/movie/top/search=Il%20grande%20Gatsby.json"]


def test_search_localized_query_keeps_cinemeta_order(monkeypatch):
    # The Italian query shares no exact/prefix match with any English name: the old
    # alphabetical tie-break put "Hong Gil Dong 2084" first. Word overlap + addon order
    # keep Cinemeta's own relevance.
    cine = _addon("Cine", "http://cine", "catalog", types=("movie",))
    other = _addon("Other", "http://other", "catalog", types=("movie",))
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [cine, other])

    def fake_get(url, **k):
        if url.startswith("http://cine"):
            return {"metas": [{"id": "tt1343092", "name": "The Great Gatsby"},
                              {"id": "tt0071577", "name": "The Great Gatsby"}]}  # fmt: skip
        return {"metas": [{"id": "kitsu:10670", "name": "Hong Gil Dong 2084"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    ids = [m["id"] for m in api.search(CFG, "Il grande Gatsby", typ="movie")]
    assert ids[0] == "tt1343092"
    assert ids[-1] == "kitsu:10670"


def test_catalog_caches_within_ttl(monkeypatch):
    cine = addons.Addon(
        base="http://cine",
        name="Cine",
        builtin=True,
        resources={"catalog": {"types": ["movie"], "idPrefixes": ["tt"]}},
    )
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [cine])
    calls = {"n": 0}

    def fake_get(url, **k):
        calls["n"] += 1
        return {"metas": [{"id": "tt1"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    api.catalog(CFG, "movie", "top")
    api.catalog(CFG, "movie", "top")
    assert calls["n"] == 1  # second call served from cache
    api.clear_cache()
    api.catalog(CFG, "movie", "top")
    assert calls["n"] == 2  # cache cleared → refetched


def test_streams_not_cached(monkeypatch):
    a = _addon("A", "http://a", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a])
    calls = {"n": 0}

    def fake_get(url, **k):
        calls["n"] += 1
        return {"streams": [{"url": "http://u1"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    api.streams(CFG, "movie", "tt1")
    api.streams(CFG, "movie", "tt1")
    assert calls["n"] == 2  # streams must never be cached


# --- hybrid backend merge (auto) ------------------------------------------


def _bh(filename):
    return {"behaviorHints": {"filename": filename}}


def test_filename_join_key_prefers_behaviorhints():
    # The key is NORMALIZED (ADR 0026): case-folded, container extension stripped. It is only
    # ever a dict/set key inside the fuse and the dedup — never displayed, never written back
    # into a stream — so normalizing it can't leak into what the user sees.
    s: Stream = {"title": "Title line\nextra", **_bh("Movie.2024.x265.mkv")}
    assert api._filename(s) == "movie.2024.x265"
    # description (protocol-current) outranks the deprecated title headline
    assert api._filename({"description": "Movie.2024.WEB\n💾 2 GB", "title": "Other"}) == (
        "movie.2024.web"
    )
    # falls back to the title's first line when neither of the two is present
    assert api._filename({"title": "Movie.2024\n👤 5"}) == "movie.2024"
    # the same release published with and without the extension joins on one key
    assert api._filename(_bh("Movie.2024.mkv")) == api._filename({"title": "Movie.2024"})


def test_merge_hybrid_fuses_by_filename():
    fn = "Inception.2010.2160p.BluRay.mkv"
    debrid = [{"url": "https://rd/u1", **_bh(fn)}, {"url": "https://rd/u2", **_bh("Other.mkv")}]
    torrents = [
        {"infoHash": "AAA", "fileIdx": 0, "sources": ["tracker:x"], api._P2P_TAG: True, **_bh(fn)},
        {"infoHash": "BBB", api._P2P_TAG: True, **_bh("PureTorrent.Only.mkv")},
    ]
    out = api._merge_hybrid(cast(list[Stream], debrid), cast(list[Stream], torrents))
    # the matched release carries BOTH the debrid url and the torrent's infoHash/fileIdx/sources
    fused = next(s for s in out if api._filename(s) == api._filename(_bh(fn)))
    assert fused["url"] == "https://rd/u1" and fused["infoHash"] == "AAA"
    assert fused["fileIdx"] == 0 and fused["sources"] == ["tracker:x"]
    # the debrid-only release stays url-only; the torrent-only release survives as pure-torrent
    assert any(s.get("url") == "https://rd/u2" and "infoHash" not in s for s in out)
    assert any(s.get("infoHash") == "BBB" and "url" not in s for s in out)
    # the internal marker is stripped from every result
    assert all(api._P2P_TAG not in s for s in out)


def test_fuse_url_and_torrent_cross_addon():
    """Ready url from Comet + pure torrent from another addon fuse by filename."""
    fn = "Movie.2020.1080p.mkv"
    streams = [
        {"name": "Comet", "url": "https://debrid/x", **_bh(fn)},
        {"name": "Other", "infoHash": "deadbeef", "fileIdx": 1, **_bh(fn)},
        {"ytId": "nope"},  # filtered before fuse in streams(); fuse itself ignores non-playable
        {"infoHash": "deadbeef", "fileIdx": 1, **_bh(fn)},  # same pure → dropped after fuse
    ]
    # Only playable rows (as streams() would pass)
    playable = [s for s in streams if s.get("url") or s.get("infoHash")]
    out = api._fuse_url_and_torrent(cast(list[Stream], playable))
    fused = next(s for s in out if s.get("url") == "https://debrid/x")
    assert fused["infoHash"] == "deadbeef" and fused["fileIdx"] == 1
    # pure absorbed into ready — no orphan duplicate
    assert sum(1 for s in out if s.get("infoHash") == "deadbeef") == 1


def test_streams_drops_unplayable_shapes(monkeypatch):
    a = _addon("A", "http://a", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a])

    def fake_get(url, **k):
        return {
            "streams": [
                {"url": "http://ok"},
                {"ytId": "abc"},
                {"externalUrl": "https://x"},
                {"infoHash": "hash1"},
            ]
        }

    monkeypatch.setattr(api, "http_get_json", fake_get)
    out = api.streams(CFG, "movie", "tt1")
    assert {s.get("url") or s.get("infoHash") for s in out} == {"http://ok", "hash1"}
    assert all(s.get("addon") == "A" for s in out)


def test_streams_stamps_addon_and_collapses_same_filename(monkeypatch):
    """Same release from two addons → one row; cached+url wins; infoHash kept."""
    a = _addon("Torrentio", "http://t", "stream")
    b = _addon("Comet", "http://c", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])
    fn = "Same.2020.1080p.mkv"

    def fake_get(url, **k):
        if url.startswith("http://t"):
            return {
                "streams": [
                    {
                        "name": "[RD+] 1080p",
                        "url": "http://rd/1",
                        "behaviorHints": {"filename": fn},
                    }
                ]
            }
        return {
            "streams": [
                {
                    "name": "1080p",
                    "infoHash": "abc",
                    "fileIdx": 0,
                    "behaviorHints": {"filename": fn},
                }
            ]
        }

    monkeypatch.setattr(api, "http_get_json", fake_get)
    out = api.streams(CFG, "movie", "tt1")
    assert len(out) == 1
    s = out[0]
    assert s["url"] == "http://rd/1" and s["infoHash"] == "abc"
    assert s["addon"] == "Torrentio"


def test_streams_no_source_returns_empty(monkeypatch):
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [])
    monkeypatch.setattr(api, "http_get_json", lambda *a, **k: pytest.fail("no fetch"))
    assert api.streams(CFG, "movie", "tt1") == []


def test_streams_auto_skips_tokenless_when_torrentio_disabled(monkeypatch):
    a = _addon("Comet", "http://comet", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a])
    urls: list[str] = []

    def fake_get(url, **k):
        urls.append(url)
        return {"streams": [{"url": "http://c1"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    cfg = Config(torrentio_base="tb", playback_backend="auto", torrentio_enabled=False)
    assert [s["url"] for s in api.streams(cfg, "movie", "tt1")] == ["http://c1"]
    assert all("torrentio" not in u for u in urls)
    assert all("P2P" not in u for u in urls)


def test_url_playable_true_on_partial(monkeypatch):
    class _Resp:
        status = 206

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert api.url_playable("http://x") is True


def test_url_playable_false_on_connection_error(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert api.url_playable("http://x") is False


def test_url_playable_optimistic_on_method_rejection(monkeypatch):
    def reject(*a, **k):
        raise urllib.error.HTTPError("http://x", 405, "no", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    assert api.url_playable("http://x") is True  # HEAD/Range rejected, resource still exists


def test_prune_meta_cache_drops_only_expired(tmp_path):
    import os
    import time as _t

    fresh = tmp_path / "fresh.json"
    fresh.write_text("{}")
    aged = tmp_path / "aged.json"
    aged.write_text("{}")
    old = _t.time() - api._META_TTL - 60
    os.utime(aged, (old, old))
    api._prune_meta_cache(tmp_path)
    assert fresh.exists() and not aged.exists()
