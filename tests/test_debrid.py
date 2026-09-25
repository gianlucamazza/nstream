"""Tests for the native debrid resolvers (TorBox/Premiumize) and the factory.

HTTP is mocked at the `debrid._urlopen` seam, so the request construction and JSON parsing
are exercised without any network. RealDebrid has no native resolver by design (ADR 0002)."""

from __future__ import annotations

import email.message
import json
import urllib.error
import urllib.request

import pytest

from nstream import debrid
from nstream.config import Config, debrid_credentials
from nstream.types import Stream


class FakeResp:
    """Minimal stand-in for a urllib response context manager."""

    def __init__(self, payload: object):
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> FakeResp:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


def _router(routes: list[tuple]):
    """Build a fake `_urlopen` dispatching by a predicate over (url, method, data).
    A route whose value is an Exception is raised; otherwise it's returned as JSON."""

    def _open(req: urllib.request.Request, timeout: float):
        url, method, data = req.full_url, req.get_method(), req.data
        for pred, result in routes:
            if pred(url, method, data):
                if isinstance(result, Exception):
                    raise result
                return FakeResp(result)
        raise AssertionError(f"richiesta non attesa: {method} {url}")

    return _open


def _cfg(base: str, backend: str = "native") -> Config:
    return Config(torrentio_base=base, playback_backend=backend)


# --- credentials + factory ----------------------------------------------


def test_debrid_credentials_parsing():
    assert debrid_credentials("sort=qualitysize|torbox=ABC") == ("torbox", "ABC")
    assert debrid_credentials("sort=qualitysize|realdebrid=XYZ") == ("realdebrid", "XYZ")
    assert debrid_credentials("sort=qualitysize") is None
    assert debrid_credentials("sort=qualitysize|torbox=") is None  # empty token → none


def test_get_resolver_torbox():
    r = debrid.get_resolver(_cfg("sort=qualitysize|torbox=TOK"))
    assert isinstance(r, debrid.TorBoxResolver)
    assert r.token == "TOK" and r.marker == "TB"


def test_get_resolver_premiumize():
    r = debrid.get_resolver(_cfg("sort=qualitysize|premiumize=TOK"))
    assert isinstance(r, debrid.PremiumizeResolver)
    assert r.marker == "PM"


def test_get_resolver_realdebrid_is_none():
    # RealDebrid is intentionally unsupported natively (ADR 0002).
    assert debrid.get_resolver(_cfg("sort=qualitysize|realdebrid=TOK")) is None


def test_get_resolver_no_token_is_none():
    assert debrid.get_resolver(_cfg("sort=qualitysize")) is None


def test_supports_native():
    assert debrid.supports_native("torbox")
    assert debrid.supports_native("premiumize")
    assert not debrid.supports_native("realdebrid")
    assert set(debrid.NATIVE_PROVIDERS) == {"torbox", "premiumize"}


# --- TorBox --------------------------------------------------------------


def test_torbox_cached_list_format(monkeypatch):
    routes = [
        (
            lambda u, m, d: "checkcached" in u,
            {"success": True, "data": [{"hash": "AABB"}, {"hash": "ccdd"}]},
        )
    ]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    got = debrid.TorBoxResolver(token="T").cached(["aabb", "ccdd", "eeff"])
    assert got == {"aabb", "ccdd"}  # lower-cased, missing one excluded


def test_torbox_cached_object_format(monkeypatch):
    routes = [(lambda u, m, d: "checkcached" in u, {"data": {"AABB": {"name": "x"}}})]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    assert debrid.TorBoxResolver(token="T").cached(["aabb"]) == {"aabb"}


def test_torbox_resolve_picks_largest_and_returns_url(monkeypatch):
    routes = [
        (lambda u, m, d: "createtorrent" in u, {"success": True, "data": {"torrent_id": 7}}),
        (
            lambda u, m, d: "mylist" in u,
            {"success": True, "data": {"files": [{"id": 0, "size": 100}, {"id": 1, "size": 999}]}},
        ),
        (lambda u, m, d: "requestdl" in u, {"success": True, "data": "https://cdn.torbox/x.mkv"}),
    ]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    url = debrid.TorBoxResolver(token="T").resolve({"infoHash": "abcd"})
    assert url == "https://cdn.torbox/x.mkv"


def test_torbox_resolve_not_cached_raises(monkeypatch):
    routes = [(lambda u, m, d: "createtorrent" in u, {"success": False, "detail": "not cached"})]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    with pytest.raises(debrid.DebridUnavailable):
        debrid.TorBoxResolver(token="T").resolve({"infoHash": "abcd"})


def test_torbox_resolve_missing_infohash_raises():
    with pytest.raises(debrid.DebridUnavailable):
        debrid.TorBoxResolver(token="T").resolve({})


# --- Premiumize ----------------------------------------------------------


def test_premiumize_cached_aligns_response(monkeypatch):
    routes = [
        (lambda u, m, d: "cache/check" in u, {"status": "success", "response": [True, False, True]})
    ]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    assert debrid.PremiumizeResolver(token="T").cached(["aa", "bb", "cc"]) == {"aa", "cc"}


def test_premiumize_resolve_picks_largest_link(monkeypatch):
    routes = [
        (
            lambda u, m, d: "directdl" in u,
            {
                "status": "success",
                "content": [
                    {"path": "a.mkv", "size": 10, "link": "L1"},
                    {"path": "b.mkv", "size": 99, "link": "L2"},
                ],
            },
        )
    ]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    assert debrid.PremiumizeResolver(token="T").resolve({"infoHash": "abcd"}) == "L2"


def test_torbox_resolve_matches_file_by_filename(monkeypatch):
    # The wanted episode (id 11) is smaller than a sample file (id 10): filename match must win
    # over both "largest" and fileIdx=0 — the series-pack correctness fix.
    seen: dict[str, str] = {}

    def fake(req, timeout):
        url = req.full_url
        if "createtorrent" in url:
            return FakeResp({"success": True, "data": {"torrent_id": 9}})
        if "mylist" in url:
            return FakeResp(
                {
                    "data": {
                        "files": [
                            {"id": 10, "name": "Sample.mkv", "size": 999},
                            {"id": 11, "name": "Show.S01E02.1080p.mkv", "size": 500},
                        ]
                    }
                }
            )
        if "requestdl" in url:
            seen["url"] = url
            return FakeResp({"data": "https://cdn/x.mkv"})
        raise AssertionError(url)

    monkeypatch.setattr(debrid, "_urlopen", fake)
    stream: Stream = {
        "infoHash": "abcd",
        "behaviorHints": {"filename": "Show.S01E02.1080p.mkv"},
        "fileIdx": 0,
    }
    assert debrid.TorBoxResolver(token="T").resolve(stream) == "https://cdn/x.mkv"
    assert "file_id=11" in seen["url"]  # matched by name, not largest (10) nor fileIdx (0)


def test_premiumize_resolve_matches_file_by_filename(monkeypatch):
    routes = [
        (
            lambda u, m, d: "directdl" in u,
            {
                "status": "success",
                "content": [
                    {"path": "/sample.mkv", "size": 999, "link": "L_big"},
                    {"path": "x/Show.S01E02.mkv", "size": 10, "link": "L_want"},
                ],
            },
        )
    ]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    stream: Stream = {
        "infoHash": "abcd",
        "behaviorHints": {"filename": "Show.S01E02.mkv"},
        "fileIdx": 0,
    }
    assert debrid.PremiumizeResolver(token="T").resolve(stream) == "L_want"


def test_premiumize_cached_auth_error_raises(monkeypatch):
    routes = [(lambda u, m, d: "cache/check" in u, {"status": "error", "message": "Not logged in"})]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    with pytest.raises(debrid.DebridUnavailable):
        debrid.PremiumizeResolver(token="T").cached(["aabb"])


def test_premiumize_resolve_uncached_raises(monkeypatch):
    routes = [(lambda u, m, d: "directdl" in u, {"status": "success", "content": []})]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    with pytest.raises(debrid.DebridUnavailable):
        debrid.PremiumizeResolver(token="T").resolve({"infoHash": "abcd"})


def test_premiumize_resolve_error_status_raises(monkeypatch):
    routes = [(lambda u, m, d: "directdl" in u, {"status": "error", "message": "bad apikey"})]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    with pytest.raises(debrid.DebridUnavailable):
        debrid.PremiumizeResolver(token="T").resolve({"infoHash": "abcd"})


# --- transport errors ----------------------------------------------------


def test_http_error_becomes_debrid_unavailable(monkeypatch):
    err = urllib.error.HTTPError(
        "https://api.torbox.app", 403, "Forbidden", email.message.Message(), None
    )
    monkeypatch.setattr(debrid, "_urlopen", _router([(lambda u, m, d: True, err)]))
    with pytest.raises(debrid.DebridUnavailable):
        debrid.TorBoxResolver(token="T").cached(["aabb"])


def test_request_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky(req, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("temporaneo")
        return FakeResp({"ok": True})

    monkeypatch.setattr(debrid, "_urlopen", flaky)
    monkeypatch.setattr(debrid.time, "sleep", lambda _s: None)
    assert debrid._request("GET", "https://x", what="t") == {"ok": True}
    assert calls["n"] == 2  # retried once


def test_request_4xx_fails_fast(monkeypatch):
    err = urllib.error.HTTPError("https://x", 404, "nf", email.message.Message(), None)
    calls = {"n": 0}

    def boom(req, timeout):
        calls["n"] += 1
        raise err

    monkeypatch.setattr(debrid, "_urlopen", boom)
    monkeypatch.setattr(debrid.time, "sleep", lambda _s: None)
    with pytest.raises(debrid.DebridUnavailable):
        debrid._request("GET", "https://x", what="t")
    assert calls["n"] == 1  # 404 not retried


# --- token never leaks (errors + logs) ------------------------------------


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "err", email.message.Message(), None)


def test_requestdl_error_never_contains_token(monkeypatch):
    """The requestdl URL carries the token in its query; a failing call must surface a
    DebridUnavailable built from `what=`, never from the URL (message, repr, or chain)."""
    token = "SECRETTOK123"
    routes = [
        (lambda u, m, d: "createtorrent" in u, {"success": True, "data": {"torrent_id": 7}}),
        (lambda u, m, d: "mylist" in u, {"data": {"files": [{"id": 1, "size": 9}]}}),
        (
            lambda u, m, d: "requestdl" in u,
            _http_error(f"{debrid._TORBOX_API}/torrents/requestdl?token={token}", 403),
        ),
    ]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    with pytest.raises(debrid.DebridUnavailable) as exc:
        debrid.TorBoxResolver(token=token).resolve({"infoHash": "abcd"})
    leakable = f"{exc.value!s} {exc.value!r} {exc.value.__cause__!r} {exc.value.__context__!r}"
    assert token not in leakable
    assert "torbox requestdl" in str(exc.value)  # the what= name, not the URL


def test_retry_log_and_exhausted_error_never_contain_token(monkeypatch):
    """Transient 5xx retries log the exception at debug and chain it into the final
    DebridUnavailable: neither path may carry the token from the query string."""
    token = "SECRETTOK123"
    msgs: list[str] = []
    monkeypatch.setattr(debrid._log, "debug", lambda m, *a: msgs.append(m % a))
    monkeypatch.setattr(debrid.time, "sleep", lambda _s: None)
    err = _http_error(f"{debrid._TORBOX_API}/torrents/requestdl?token={token}", 500)
    monkeypatch.setattr(debrid, "_urlopen", _router([(lambda u, m, d: True, err)]))
    with pytest.raises(debrid.DebridUnavailable) as exc:
        debrid._request(
            "GET",
            f"{debrid._TORBOX_API}/torrents/requestdl",
            what="torbox requestdl",
            params={"token": token},
            retries=1,
        )
    assert token not in str(exc.value) and token not in repr(exc.value.__cause__)
    assert msgs and all(token not in m for m in msgs)


def test_log_redaction_covers_native_token_carriers():
    """Belt and braces: even if a native-debrid URL or auth header ever reached the log,
    log.py's redaction must scrub both carriers used here (?token= query, Bearer header)."""
    from nstream import log

    url = f"{debrid._TORBOX_API}/torrents/requestdl?token=SECRETTOK123&torrent_id=7"
    assert "SECRETTOK123" not in log.redact(url)
    assert log.redact(url) == "<url>"
    hdr = "Authorization: Bearer SECRETTOK123"
    assert "SECRETTOK123" not in log.redact(hdr)
    assert "Bearer <redacted>" in log.redact(hdr)


# --- selftest ------------------------------------------------------------


def test_selftest_no_resolver():
    out = debrid.selftest(_cfg("sort=qualitysize|realdebrid=T"), "abcd")
    assert "nessun resolver nativo" in out


def test_selftest_masks_token_in_url(monkeypatch):
    routes = [
        (lambda u, m, d: "checkcached" in u, {"data": [{"hash": "abcd"}]}),
        (lambda u, m, d: "createtorrent" in u, {"success": True, "data": {"torrent_id": 1}}),
        (lambda u, m, d: "mylist" in u, {"data": {"files": [{"id": 5, "size": 10}]}}),
        (lambda u, m, d: "requestdl" in u, {"data": "https://cdn.torbox/v.mkv?token=SECRET"}),
    ]
    monkeypatch.setattr(debrid, "_urlopen", _router(routes))
    out = debrid.selftest(_cfg("sort=qualitysize|torbox=T"), "ABCD")
    assert "cached(): sì" in out
    assert "https://cdn.torbox/v.mkv" in out and "SECRET" not in out  # query masked
    assert "OK:" in out
