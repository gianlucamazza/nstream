"""Unit tests for the low-level HTTP-JSON client (retry/backoff/Retry-After semantics)."""

from __future__ import annotations

import gzip
import json
import urllib.error

import pytest

from nstream import net


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


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "boom", headers or {}, None)


@pytest.fixture
def sleeps(monkeypatch):
    """Capture time.sleep calls; no real waiting in tests."""
    waited: list[float] = []
    monkeypatch.setattr(net.time, "sleep", lambda s: waited.append(s))
    return waited


# --- success paths ----------------------------------------------------------


def test_success_first_try_plain(monkeypatch, sleeps):
    body = json.dumps({"ok": 1}).encode()
    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    assert net.http_get_json("http://x", what="t") == {"ok": 1}
    assert sleeps == []  # no retry, no wait


def test_success_gzip_via_header(monkeypatch):
    body = gzip.compress(json.dumps({"ok": 2}).encode())
    resp = _Resp(body, {"Content-Encoding": "gzip"})
    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: resp)
    assert net.http_get_json("http://x", what="t") == {"ok": 2}


def test_success_gzip_sniffed_without_header(monkeypatch):
    # Magic-bytes sniffing: gunzips even when the server omits Content-Encoding.
    body = gzip.compress(json.dumps({"ok": 3}).encode())
    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    assert net.http_get_json("http://x", what="t") == {"ok": 3}


# --- retryable vs non-retryable statuses -------------------------------------


def test_5xx_retried_then_success(monkeypatch, sleeps):
    body = json.dumps({"ok": 1}).encode()
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(503)
        return _Resp(body)

    monkeypatch.setattr(net.urllib.request, "urlopen", flaky)
    assert net.http_get_json("http://x", what="t") == {"ok": 1}
    assert calls["n"] == 2 and len(sleeps) == 1


def test_4xx_fails_immediately_no_retry(monkeypatch, sleeps):
    calls = {"n": 0}

    def not_found(*a, **k):
        calls["n"] += 1
        raise _http_error(404)

    monkeypatch.setattr(net.urllib.request, "urlopen", not_found)
    with pytest.raises(net.NetworkError, match="HTTP 404"):
        net.http_get_json("http://x", what="t")
    assert calls["n"] == 1 and sleeps == []


def test_error_message_never_includes_url(monkeypatch):
    # The URL may embed a debrid token: only the `what=` label may appear.
    monkeypatch.setattr(
        net.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(_http_error(403))
    )
    with pytest.raises(net.NetworkError) as exc_info:
        net.http_get_json("http://host/resolve/realdebrid/SECRET", what="addon")
    assert "SECRET" not in str(exc_info.value) and "addon" in str(exc_info.value)


# --- 429 / Retry-After --------------------------------------------------------


def test_429_honours_retry_after(monkeypatch, sleeps):
    body = json.dumps({"ok": 1}).encode()
    calls = {"n": 0}

    def throttled(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, {"Retry-After": "7"})
        return _Resp(body)

    monkeypatch.setattr(net.urllib.request, "urlopen", throttled)
    assert net.http_get_json("http://x", what="t") == {"ok": 1}
    assert sleeps == [7.0]  # header value used verbatim, not the exponential backoff


def test_429_retry_after_capped(monkeypatch, sleeps):
    body = json.dumps({"ok": 1}).encode()
    calls = {"n": 0}

    def throttled(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429, {"Retry-After": "9999"})
        return _Resp(body)

    monkeypatch.setattr(net.urllib.request, "urlopen", throttled)
    net.http_get_json("http://x", what="t")
    assert sleeps == [30.0]  # util._RETRY_AFTER_CAP


def test_429_without_header_falls_back_to_backoff(monkeypatch, sleeps):
    body = json.dumps({"ok": 1}).encode()
    calls = {"n": 0}
    monkeypatch.setattr(net.util, "backoff", lambda attempt: 0.5 * (2**attempt))

    def throttled(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(429)
        return _Resp(body)

    monkeypatch.setattr(net.urllib.request, "urlopen", throttled)
    net.http_get_json("http://x", what="t")
    assert sleeps == [0.5]


# --- exhaustion & backoff ------------------------------------------------------


def test_urlerror_retries_until_exhausted(monkeypatch, sleeps):
    calls = {"n": 0}

    def down(*a, **k):
        calls["n"] += 1
        raise urllib.error.URLError("down")

    monkeypatch.setattr(net.urllib.request, "urlopen", down)
    with pytest.raises(net.NetworkError, match="dopo 3 tentativi"):
        net.http_get_json("http://x", what="t", retries=2)
    assert calls["n"] == 3  # retries=2 → 3 attempts total
    assert len(sleeps) == 2  # no sleep after the last attempt


def test_timeout_retries_like_urlerror(monkeypatch, sleeps):
    def slow(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(net.urllib.request, "urlopen", slow)
    with pytest.raises(net.NetworkError, match="dopo 4 tentativi"):
        net.http_get_json("http://x", what="t")  # default retries=3
    assert len(sleeps) == 3


def test_backoff_grows_between_retries(monkeypatch, sleeps):
    def down(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(net.urllib.request, "urlopen", down)
    with pytest.raises(net.NetworkError):
        net.http_get_json("http://x", what="t")
    # Real util.backoff: 0.5*2^n + jitter[0,0.3] → strictly increasing across attempts.
    assert len(sleeps) == 3
    assert sleeps == sorted(sleeps) and sleeps[0] < sleeps[1] < sleeps[2]
    for n, s in enumerate(sleeps):
        assert 0.5 * 2**n <= s <= 0.5 * 2**n + 0.3


# --- bad body: never retried ----------------------------------------------------


def test_bad_json_fails_immediately(monkeypatch, sleeps):
    calls = {"n": 0}

    def garbage(*a, **k):
        calls["n"] += 1
        return _Resp(b"not json")

    monkeypatch.setattr(net.urllib.request, "urlopen", garbage)
    with pytest.raises(net.NetworkError, match="non valida"):
        net.http_get_json("http://x", what="t")
    assert calls["n"] == 1 and sleeps == []


def test_corrupt_gzip_fails_immediately(monkeypatch, sleeps):
    resp = _Resp(b"\x1f\x8bnot-gzip", {"Content-Encoding": "gzip"})
    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: resp)
    with pytest.raises(net.NetworkError, match="non valida"):
        net.http_get_json("http://x", what="t")
    assert sleeps == []


# --- url_playable ----------------------------------------------------------------


def test_url_playable_true_below_400(monkeypatch):
    class _R:
        status = 206

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: _R())
    assert net.url_playable("http://x") is True


@pytest.mark.parametrize("code,expected", [(403, True), (405, True), (416, True), (404, False)])
def test_url_playable_http_errors(monkeypatch, code, expected):
    def reject(*a, **k):
        raise _http_error(code)

    monkeypatch.setattr(net.urllib.request, "urlopen", reject)
    assert net.url_playable("http://x") is expected


def test_url_playable_false_on_connection_error(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(net.urllib.request, "urlopen", boom)
    assert net.url_playable("http://x") is False


# --- probe_url: three-state classification (ADR 0025) ----------------------------


class _ProbeResp:
    """A probe response with headers, mimicking urlopen's context manager."""

    def __init__(self, status: int, headers: dict | None = None):
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, status: int, headers: dict | None = None):
    monkeypatch.setattr(net.urllib.request, "urlopen", lambda *a, **k: _ProbeResp(status, headers))


def test_probe_live_when_size_matches(monkeypatch):
    _serve(monkeypatch, 206, {"Content-Range": "bytes 0-0/7686000000"})
    probe = net.probe_url("http://x", expected_bytes=7_600_000_000)
    assert probe.state == net.LIVE
    assert probe.usable and not probe.dead


def test_probe_gone_when_placeholder_served(monkeypatch):
    """A revoked debrid link answers 200 with a few-KB placeholder, not the movie."""
    _serve(monkeypatch, 206, {"Content-Range": "bytes 0-0/40000"})
    probe = net.probe_url("http://x", expected_bytes=7_000_000_000)
    assert probe.state == net.GONE
    assert probe.dead and not probe.usable
    assert "annunciati" in probe.reason


def test_probe_live_when_size_unknown(monkeypatch):
    """No Content-Range/Length (chunked): nothing to judge, so don't invent a verdict."""
    _serve(monkeypatch, 206, {})
    assert net.probe_url("http://x", expected_bytes=7_000_000_000).state == net.LIVE


def test_probe_live_when_expected_unknown(monkeypatch):
    """A small file is only suspicious against an announced size."""
    _serve(monkeypatch, 200, {"Content-Length": "40000"})
    assert net.probe_url("http://x").state == net.LIVE


def test_probe_gone_on_404(monkeypatch):
    monkeypatch.setattr(
        net.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(_http_error(404))
    )
    probe = net.probe_url("http://x")
    assert probe.dead and probe.status == 404


@pytest.mark.parametrize("code", [403, 405, 416])
def test_probe_unknown_but_usable_on_method_rejection(monkeypatch, code):
    def reject(*a, **k):
        raise _http_error(code)

    monkeypatch.setattr(net.urllib.request, "urlopen", reject)
    probe = net.probe_url("http://x")
    assert probe.state == net.UNKNOWN
    assert probe.usable and not probe.dead  # benefit of the doubt, never denylisted


def test_probe_unknown_on_server_error(monkeypatch):
    def boom(*a, **k):
        raise _http_error(503)

    monkeypatch.setattr(net.urllib.request, "urlopen", boom)
    probe = net.probe_url("http://x")
    assert probe.state == net.UNKNOWN
    assert not probe.usable and not probe.dead  # falls back, but isn't remembered


def test_probe_unknown_on_transport_error(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(net.urllib.request, "urlopen", boom)
    probe = net.probe_url("http://x")
    assert not probe.usable and not probe.dead


def test_served_total_prefers_content_range():
    assert net._served_total({"Content-Range": "bytes 0-0/123"}, 206) == 123
    assert net._served_total({"Content-Length": "99"}, 200) == 99
    assert net._served_total({"Content-Length": "1"}, 206) is None  # a 1-byte slice, not the total
    assert net._served_total({}, 200) is None


def test_probe_partial_uncached_is_not_gone(monkeypatch):
    """`[RD download]`: the provider is still transferring the file, so the partial size must
    read as "not yet", never as removed — banning it would lock out a title about to work."""
    _serve(monkeypatch, 206, {"Content-Range": "bytes 0-0/2097152"})
    probe = net.probe_url("http://x", expected_bytes=7_000_000_000, complete=False)
    assert probe.state == net.UNKNOWN
    assert not probe.dead and not probe.usable  # fall back now, remember nothing
    assert "trasferimento in corso" in probe.reason


def test_probe_partial_cached_is_gone(monkeypatch):
    """A release advertised as cached is supposed to be a finished file: short = removed."""
    _serve(monkeypatch, 206, {"Content-Range": "bytes 0-0/2097152"})
    assert net.probe_url("http://x", expected_bytes=7_000_000_000, complete=True).dead is True
