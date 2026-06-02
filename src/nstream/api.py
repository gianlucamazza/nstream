"""HTTP access to the Stremio addon APIs (Cinemeta, Torrentio) with retry/backoff.

All requests share a timeout and retry only transient failures (timeouts, connection
errors, HTTP 429/5xx) with exponential backoff, honouring ``Retry-After``. Client
errors (4xx) are not retried. Error messages never include the request URL, so the
Real-Debrid token embedded in Torrentio URLs is never leaked to logs.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Config, Meta, Stream, Subtitle, Video

UA = "Mozilla/5.0 nstream"
TIMEOUT = 20.0
_BACKOFF_BASE = 0.5
_RETRY_AFTER_CAP = 30.0


class NetworkError(Exception):
    """A request failed after exhausting retries, or hit a non-retryable status."""


def _backoff(attempt: int) -> float:
    return _BACKOFF_BASE * (2**attempt) + random.uniform(0.0, 0.3)


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if value and value.isdigit():
        return min(float(value), _RETRY_AFTER_CAP)
    return None


def http_get_json(url: str, *, what: str = "richiesta", retries: int = 3) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # Don't retry client errors (auth, not found, bad config).
            if e.code != 429 and not (500 <= e.code < 600):
                raise NetworkError(f"{what}: HTTP {e.code}") from None
            last_exc = e
            wait = _retry_after(e)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
            wait = None
        except json.JSONDecodeError as e:
            raise NetworkError(f"{what}: risposta non valida") from e

        if attempt >= retries:
            break
        time.sleep(wait if wait is not None else _backoff(attempt))

    raise NetworkError(f"{what}: rete non raggiungibile dopo {retries + 1} tentativi") from last_exc


def search(cfg: Config, query: str) -> list[Meta]:
    metas: list[Meta] = []
    q = urllib.parse.quote(query)
    for typ in ("movie", "series"):
        url = f"{cfg.cinemeta}/catalog/{typ}/top/search={q}.json"
        metas.extend(http_get_json(url, what=f"ricerca {typ}").get("metas", []))
    return metas


def catalog(
    cfg: Config, typ: str, cat: str = "top", *, genre: str | None = None, skip: int = 0
) -> list[Meta]:
    # Cinemeta extras are path segments, not query params: /catalog/{typ}/{cat}/genre=X/skip=N.json
    extras = ""
    if genre:
        extras += f"/genre={urllib.parse.quote(genre)}"
    if skip:
        extras += f"/skip={skip}"
    url = f"{cfg.cinemeta}/catalog/{typ}/{cat}{extras}.json"
    return http_get_json(url, what="catalogo").get("metas", [])


def episodes(cfg: Config, series_id: str) -> list[Video]:
    data = http_get_json(f"{cfg.cinemeta}/meta/series/{series_id}.json", what="episodi")
    vids: list[Video] = [v for v in data.get("meta", {}).get("videos", []) if v.get("season")]
    vids.sort(key=lambda v: (v.get("season", 0), v.get("episode", 0)))
    return vids


def streams(cfg: Config, typ: str, video_id: str) -> list[Stream]:
    # NB: this URL embeds the Real-Debrid token — keep it out of error messages.
    base = urllib.parse.quote(cfg.torrentio_base, safe="=|")
    url = f"https://torrentio.strem.fun/{base}/stream/{typ}/{video_id}.json"
    return http_get_json(url, what="stream").get("streams", [])


def subtitles(cfg: Config, typ: str, video_id: str) -> list[Subtitle]:
    url = f"{cfg.opensubtitles}/subtitles/{typ}/{video_id}.json"
    return http_get_json(url, what="sottotitoli").get("subtitles", [])
