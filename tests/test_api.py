"""Unit tests for api resource dispatch and aggregation across addons."""

from __future__ import annotations

import pytest

from nstream import addons, api
from nstream.config import Config

CFG = Config(torrentio_base="tb")


@pytest.fixture(autouse=True)
def _clear_meta_cache():
    api.clear_cache()
    yield
    api.clear_cache()


def _addon(name, base, resource, types=("movie",), idp=("tt",), catalogs=()):
    return addons.Addon(
        base=base,
        name=name,
        resources={resource: {"types": list(types), "idPrefixes": list(idp)}},
        catalogs=catalogs,
    )


def test_streams_aggregate_and_dedup(monkeypatch):
    a = _addon("A", "http://a", "stream")
    b = _addon("B", "http://b", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])

    def fake_get(url, **k):
        if url.startswith("http://a"):
            return {"streams": [{"url": "u1"}, {"url": "u2"}]}
        return {"streams": [{"url": "u2"}, {"url": "u3"}]}  # u2 duplicate

    monkeypatch.setattr(api, "http_get_json", fake_get)
    assert [s["url"] for s in api.streams(CFG, "movie", "tt1")] == ["u1", "u2", "u3"]


def test_streams_one_addon_fails_does_not_block(monkeypatch, capsys):
    a = _addon("A", "http://a", "stream")
    b = _addon("B", "http://b", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])

    def fake_get(url, **k):
        if url.startswith("http://a"):
            raise api.NetworkError("down")
        return {"streams": [{"url": "u3"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    assert [s["url"] for s in api.streams(CFG, "movie", "tt1")] == ["u3"]
    # No per-addon error spam (avoids the double "stream … / nessuno stream" message).
    assert capsys.readouterr().err == ""


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


# --- gzip, browse, cache (Fase 1p) -----------------------------------------

import gzip as _gzip  # noqa: E402
import json as _json  # noqa: E402


class _Resp:
    """Minimal urlopen() context-manager stand-in."""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_get_json_plain(monkeypatch):
    body = _json.dumps({"ok": 1}).encode()
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    assert api.http_get_json("http://x", what="t") == {"ok": 1}


def test_http_get_json_gzip(monkeypatch):
    body = _gzip.compress(_json.dumps({"ok": 2}).encode())
    resp = _Resp(body, {"Content-Encoding": "gzip"})
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: resp)
    assert api.http_get_json("http://x", what="t") == {"ok": 2}


def test_http_get_json_corrupt_gzip_raises(monkeypatch):
    resp = _Resp(b"\x1f\x8bnot-gzip", {"Content-Encoding": "gzip"})
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *a, **k: resp)
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
    cine = addons.Addon(**{**cine.__dict__, "builtin": True})
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [cine])

    def fake_get(url, **k):
        typ = "movie" if "/movie/" in url else "series"
        return {"metas": [{"id": f"{typ}1", "name": typ}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    ids = [m["id"] for m in api.browse(CFG, "top")]
    assert set(ids) == {"movie1", "series1"}


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
        return {"streams": [{"url": "u1"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    api.streams(CFG, "movie", "tt1")
    api.streams(CFG, "movie", "tt1")
    assert calls["n"] == 2  # streams must never be cached
