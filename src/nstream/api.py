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

from . import addons
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


def _dedup(items: list, key) -> list:
    seen: set = set()
    out: list = []
    for it in items:
        k = key(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def search(cfg: Config, query: str) -> list[Meta]:
    q = urllib.parse.quote(query)
    metas: list[Meta] = []
    for addon in addons.effective_addons(cfg):
        for typ in ("movie", "series"):
            if not addons.serves(addon, "catalog", typ):
                continue
            url = f"{addon.base}/catalog/{typ}/top/search={q}.json"
            try:
                metas.extend(
                    http_get_json(url, what=f"ricerca {typ} ({addon.name})").get("metas", [])
                )
            except NetworkError:
                continue
    return _dedup(metas, lambda m: m.get("id") or id(m))


def catalog(
    cfg: Config, typ: str, cat: str = "top", *, genre: str | None = None, skip: int = 0
) -> list[Meta]:
    # Stremio extras are path segments, not query params: /catalog/{typ}/{cat}/genre=X/skip=N.json
    extras = ""
    if genre:
        extras += f"/genre={urllib.parse.quote(genre)}"
    if skip:
        extras += f"/skip={skip}"
    metas: list[Meta] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "catalog", typ):
            continue
        # Built-in Cinemeta has these catalogs; a user addon must declare them.
        if not addon.builtin and (typ, cat) not in addon.catalogs:
            continue
        url = f"{addon.base}/catalog/{typ}/{cat}{extras}.json"
        try:
            metas.extend(http_get_json(url, what=f"catalogo ({addon.name})").get("metas", []))
        except NetworkError:
            continue
    return _dedup(metas, lambda m: m.get("id") or id(m))


def meta(cfg: Config, typ: str, video_id: str) -> dict:
    """Full Cinemeta-style meta object for an id (first meta addon that answers)."""
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "meta", typ, video_id):
            continue
        try:
            data = http_get_json(
                f"{addon.base}/meta/{typ}/{video_id}.json", what=f"meta ({addon.name})"
            )
        except NetworkError:
            continue
        obj = data.get("meta")
        if obj:
            return obj
    return {}


def episodes(cfg: Config, series_id: str) -> list[Video]:
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "meta", "series", series_id):
            continue
        try:
            data = http_get_json(
                f"{addon.base}/meta/series/{series_id}.json", what=f"episodi ({addon.name})"
            )
        except NetworkError:
            continue
        vids: list[Video] = [v for v in data.get("meta", {}).get("videos", []) if v.get("season")]
        if vids:
            vids.sort(key=lambda v: (v.get("season", 0), v.get("episode", 0)))
            return vids
    return []


def streams(cfg: Config, typ: str, video_id: str) -> list[Stream]:
    out: list[Stream] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "stream", typ, video_id):
            continue
        # NB: a stream addon's base may embed the Real-Debrid token — `what` uses the
        # addon name, never the URL, so errors never leak it.
        url = f"{addon.base}/stream/{typ}/{video_id}.json"
        try:
            out.extend(http_get_json(url, what=f"stream ({addon.name})").get("streams", []))
        except NetworkError:
            # Best-effort like the other aggregators: a failing addon is skipped;
            # the caller reports "nessuno stream disponibile" if nothing is found.
            continue
    return _dedup(out, lambda s: s.get("url") or id(s))


def subtitles(cfg: Config, typ: str, video_id: str) -> list[Subtitle]:
    out: list[Subtitle] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "subtitles", typ, video_id):
            continue
        url = f"{addon.base}/subtitles/{typ}/{video_id}.json"
        try:
            out.extend(http_get_json(url, what=f"sottotitoli ({addon.name})").get("subtitles", []))
        except NetworkError:
            continue
    return _dedup(out, lambda s: s.get("url") or s.get("id") or id(s))
