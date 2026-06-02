"""Unit tests for api resource dispatch and aggregation across addons."""

from __future__ import annotations

import pytest

from nstream import addons, api
from nstream.config import Config

CFG = Config(torrentio_base="tb")


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


def test_streams_one_addon_fails_does_not_block(monkeypatch):
    a = _addon("A", "http://a", "stream")
    b = _addon("B", "http://b", "stream")
    monkeypatch.setattr(api.addons, "effective_addons", lambda cfg: [a, b])

    def fake_get(url, **k):
        if url.startswith("http://a"):
            raise api.NetworkError("down")
        return {"streams": [{"url": "u3"}]}

    monkeypatch.setattr(api, "http_get_json", fake_get)
    assert [s["url"] for s in api.streams(CFG, "movie", "tt1")] == ["u3"]


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
