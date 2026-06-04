"""Low-level HTTP-JSON client shared by the addon dispatch (`api`) and the native debrid
resolver. A retrying GET→JSON (gzip-aware, exponential backoff honouring `Retry-After`) plus a
cheap reachability probe.

Split out of `api` so `addons` can fetch manifests without importing `api`: `api` dispatches
over `addons.effective_addons`, so the legitimate direction is `api → addons`; keeping the HTTP
primitive in a module below both breaks the former `addons ↔ api` import cycle. Leaf: imports
only `log`/`util` + stdlib. Error messages never include the request URL (which may embed a
debrid token); callers pass a `what=` label instead.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request

from . import log, util

_log = log.get_logger("net")

UA = "Mozilla/5.0 nstream"
TIMEOUT = 20.0


class NetworkError(Exception):
    """A request failed after exhausting retries, or hit a non-retryable status."""


def url_playable(url: str, *, timeout: float = 6.0) -> bool:
    """Best-effort reachability check for a ready (debrid) stream url: True if the server
    serves the first byte, False on a clear failure (dead/expired link, 4xx/5xx, connection
    error). Conservative — a HEAD/Range rejection (403/405/416) still counts as reachable, so
    we only veto clear misses. Real-Debrid can't reliably report a cached-miss, so this catches
    dead links and resolve errors, not every non-cached case."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except urllib.error.HTTPError as e:
        return e.code in (403, 405, 416)  # method/range not allowed, but the resource exists
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return False


def _read_json(resp) -> dict:
    """Read a urllib response body, transparently gunzipping when needed."""
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower() if resp.headers else ""
    if "gzip" in enc or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def http_get_json(url: str, *, what: str = "richiesta", retries: int = 3) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return _read_json(resp)
        except urllib.error.HTTPError as e:
            # Don't retry client errors (auth, not found, bad config).
            if e.code != 429 and not (500 <= e.code < 600):
                raise NetworkError(f"{what}: HTTP {e.code}") from None
            last_exc = e
            wait = util.retry_after(e)
        except (json.JSONDecodeError, gzip.BadGzipFile, EOFError) as e:
            # Bad body (incl. corrupt gzip) — caught before the broad OSError branch
            # since BadGzipFile is an OSError; retrying wouldn't help.
            raise NetworkError(f"{what}: risposta non valida") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
            wait = None

        if attempt >= retries:
            break
        _log.debug("%s: tentativo %d fallito (%s), retry", what, attempt + 1, last_exc)
        time.sleep(wait if wait is not None else util.backoff(attempt))

    _log.warning("%s: rete non raggiungibile dopo %d tentativi", what, retries + 1)
    raise NetworkError(f"{what}: rete non raggiungibile dopo {retries + 1} tentativi") from last_exc
