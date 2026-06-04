"""HTTP access to the Stremio addon APIs (Cinemeta, Torrentio) with retry/backoff.

All requests share a timeout and retry only transient failures (timeouts, connection
errors, HTTP 429/5xx) with exponential backoff, honouring ``Retry-After``. Client
errors (4xx) are not retried. Error messages never include the request URL, so the
Real-Debrid token embedded in Torrentio URLs is never leaked to logs.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import addons, log, util
from .config import Config, Meta, Stream, Subtitle, Video

_log = log.get_logger("api")

UA = "Mozilla/5.0 nstream"
TIMEOUT = 20.0
_MAX_WORKERS = 8
# In-process TTL cache for token-free metadata (search/catalog/meta/episodes). Each
# CLI run is a fresh process, but the home TUI / browse / pick loops live in one
# process, so this makes back-navigation and re-browse within a session instant.
# Stream/subtitle responses are NOT cached (RD availability changes; their URLs hold
# the token).
_META_TTL = 600.0
_meta_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


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


def _dedup(items: list, key) -> list:
    seen: set = set()
    out: list = []
    for it in items:
        k = key(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _gather(tasks: list[Callable[[], list]]) -> list:
    """Run per-addon/per-type fetch tasks concurrently, flattening results in task
    order (so the built-in providers keep priority for dedup). A task raising
    NetworkError contributes nothing (best-effort aggregation). One task → run inline
    (no thread overhead)."""
    if not tasks:
        return []
    if len(tasks) == 1:
        results = [_safe(tasks[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tasks))) as ex:
            futures = [ex.submit(_safe, t) for t in tasks]
            results = [f.result() for f in futures]  # in submit order
    out: list = []
    for r in results:
        out.extend(r)
    return out


def _safe(task: Callable[[], list]) -> list:
    try:
        return task()
    except NetworkError as e:
        # The message carries the addon's `what=` label (no clear URL → redaction-safe),
        # so a silently-skipped addon is still traceable with --debug.
        _log.debug("addon saltato: %s", e)
        return []


def clear_cache() -> None:
    """Drop the in-process metadata cache (used by tests)."""
    with _cache_lock:
        _meta_cache.clear()


def _cached_json(url: str, *, what: str) -> dict:
    """`http_get_json` with a short in-process TTL cache, for token-free metadata
    endpoints (Cinemeta search/catalog/meta/episodes). Never used for streams/subs."""
    now = time.monotonic()
    with _cache_lock:
        hit = _meta_cache.get(url)
        if hit and now - hit[0] < _META_TTL:
            return hit[1]
    data = http_get_json(url, what=what)
    with _cache_lock:
        # Prune expired entries opportunistically to bound growth.
        for k in [k for k, (ts, _) in _meta_cache.items() if now - ts >= _META_TTL]:
            del _meta_cache[k]
        _meta_cache[url] = (now, data)
    return data


def _catalog_tasks(cfg: Config, typ: str, path: str, what: str) -> list[Callable[[], list]]:
    """Build one cached fetch task per addon serving `catalog` for this type."""
    tasks: list[Callable[[], list]] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "catalog", typ):
            continue
        url = f"{addon.base}/catalog/{typ}/{path}.json"
        tasks.append(
            lambda url=url, name=addon.name: _cached_json(url, what=f"{what} ({name})").get(
                "metas", []
            )
        )
    return tasks


def search(cfg: Config, query: str) -> list[Meta]:
    q = urllib.parse.quote(query)
    tasks: list[Callable[[], list]] = []
    for typ in ("movie", "series"):
        tasks += _catalog_tasks(cfg, typ, f"top/search={q}", f"ricerca {typ}")
    return _dedup(_gather(tasks), lambda m: m.get("id") or id(m))


def _catalog_addon_tasks(cfg: Config, typ: str, cat: str, extras: str) -> list[Callable[[], list]]:
    tasks: list[Callable[[], list]] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "catalog", typ):
            continue
        # Built-in Cinemeta has these catalogs; a user addon must declare them.
        if not addon.builtin and (typ, cat) not in addon.catalogs:
            continue
        url = f"{addon.base}/catalog/{typ}/{cat}{extras}.json"
        tasks.append(
            lambda url=url, name=addon.name: _cached_json(url, what=f"catalogo ({name})").get(
                "metas", []
            )
        )
    return tasks


def _extras(genre: str | None, skip: int) -> str:
    # Stremio extras are path segments, not query params: /catalog/{typ}/{cat}/genre=X/skip=N.json
    extras = ""
    if genre:
        extras += f"/genre={urllib.parse.quote(genre)}"
    if skip:
        extras += f"/skip={skip}"
    return extras


def catalog(
    cfg: Config, typ: str, cat: str = "top", *, genre: str | None = None, skip: int = 0
) -> list[Meta]:
    tasks = _catalog_addon_tasks(cfg, typ, cat, _extras(genre, skip))
    return _dedup(_gather(tasks), lambda m: m.get("id") or id(m))


def browse(cfg: Config, cat: str = "top", *, genre: str | None = None, skip: int = 0) -> list[Meta]:
    """Movies + series for a catalog, fetched concurrently (used by the browse menu)."""
    extras = _extras(genre, skip)
    tasks = _catalog_addon_tasks(cfg, "movie", cat, extras) + _catalog_addon_tasks(
        cfg, "series", cat, extras
    )
    return _dedup(_gather(tasks), lambda m: m.get("id") or id(m))


def meta(cfg: Config, typ: str, video_id: str) -> dict:
    """Full Cinemeta-style meta object for an id (first meta addon that answers)."""
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "meta", typ, video_id):
            continue
        try:
            data = _cached_json(
                f"{addon.base}/meta/{typ}/{video_id}.json", what=f"meta ({addon.name})"
            )
        except NetworkError:
            continue
        obj = data.get("meta")
        if obj:
            return obj
    return {}


def _meta_disk_path(typ: str, video_id: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    h = hashlib.sha256(f"{typ}:{video_id}".encode()).hexdigest()
    return Path(base) / "nstream" / "meta" / f"{h}.json"


def meta_cached_disk(cfg: Config, typ: str, video_id: str) -> dict:
    """`meta()` with an on-disk TTL cache. The preview subcommand spawns a fresh process
    per focused row, so the in-process cache never helps there; persisting meta (which is
    token-free, unlike streams) gives instant hits across processes. Best-effort: a cache
    miss or write failure just falls through to a normal fetch."""
    path = _meta_disk_path(typ, video_id)
    cached = util.load_json(path, {})
    ts = cached.get("ts")
    obj = cached.get("meta")
    if isinstance(ts, int | float) and time.time() - ts < _META_TTL and isinstance(obj, dict):
        return obj
    fresh = meta(cfg, typ, video_id)
    if fresh:
        with contextlib.suppress(OSError):
            util.atomic_write(
                path,
                lambda f: json.dump({"ts": time.time(), "meta": fresh}, f, ensure_ascii=False),
                prefix=".meta-",
            )
    return fresh


def episodes(cfg: Config, series_id: str) -> list[Video]:
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "meta", "series", series_id):
            continue
        try:
            data = _cached_json(
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
    # NB: a stream addon's base may embed the Real-Debrid token — `what` uses the
    # addon name, never the URL, so errors never leak it. NOT cached (RD availability
    # changes between calls, and the URLs carry the token).
    tasks: list[Callable[[], list]] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "stream", typ, video_id):
            continue
        url = f"{addon.base}/stream/{typ}/{video_id}.json"
        tasks.append(
            lambda url=url, name=addon.name: http_get_json(url, what=f"stream ({name})").get(
                "streams", []
            )
        )
    # Hybrid "auto" backend: the main query carries the debrid token (cached urls); fetch the
    # token-less Torrentio variant too (pure-torrent infoHash) and merge, so a release can be
    # played via debrid AND fall back to local P2P. Run it as another concurrent task.
    if cfg.playback_backend == "auto":
        tl = f"{addons.torrentio_token_less(cfg)}/stream/{typ}/{video_id}.json"
        tasks.append(
            lambda tl=tl: _tagged(
                http_get_json(tl, what="stream (Torrentio P2P)").get("streams", [])
            )
        )
        gathered = _gather(tasks)  # flat list of streams, task order preserved
        debrid = [s for s in gathered if not _is_p2p(s)]
        torrents = [s for s in gathered if _is_p2p(s)]
        return _dedup(_merge_hybrid(debrid, torrents), _stream_key)
    return _dedup(_gather(tasks), _stream_key)


# Marker key (private, stripped before returning) tagging the pure-torrent batch in the
# hybrid merge so it can be told apart from the debrid batch after the concurrent gather.
_P2P_TAG = "__p2p__"


def _tagged(streams: list[Stream]) -> list[Stream]:
    for s in streams:
        s[_P2P_TAG] = True
    return streams


def _is_p2p(s: Stream) -> bool:
    return bool(s.get(_P2P_TAG))


def _filename(s: Stream) -> str:
    """Torrentio's per-file name (behaviorHints.filename) — identical across the debrid and
    token-less queries for the same release, so it's the join key for the hybrid merge.
    Falls back to the title's first line when absent."""
    fn = (s.get("behaviorHints") or {}).get("filename")
    return fn or (s.get("title") or "").split("\n", 1)[0].strip()


def _merge_hybrid(debrid: list[Stream], torrents: list[Stream]) -> list[Stream]:
    """Fuse the debrid (url) and token-less (infoHash) Torrentio results by filename: a matched
    release gets both a debrid `url` and the torrent's `infoHash`/`fileIdx`/`sources`, so it can
    play via debrid and fall back to local P2P. Token-less-only releases are kept as pure-torrent;
    the internal _P2P_TAG marker is stripped from everything."""
    by_name = {_filename(s): s for s in torrents}
    out: list[Stream] = []
    for s in debrid:
        t = by_name.pop(_filename(s), None)
        if t:
            for k in ("infoHash", "fileIdx", "sources"):
                if k in t and k not in s:
                    s[k] = t[k]
        out.append(s)
    out.extend(by_name.values())  # pure-torrent releases with no debrid match
    for s in out:
        s.pop(_P2P_TAG, None)
    return out


def _stream_key(s: Stream) -> object:
    """Dedup key: ready url when present, else the torrent's (infoHash, fileIdx) so
    pure-torrent streams (no url) survive instead of collapsing onto one another."""
    if s.get("url"):
        return s["url"]
    if s.get("infoHash"):
        return (s["infoHash"], s.get("fileIdx"))
    return id(s)


def subtitles(cfg: Config, typ: str, video_id: str) -> list[Subtitle]:
    tasks: list[Callable[[], list]] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "subtitles", typ, video_id):
            continue
        url = f"{addon.base}/subtitles/{typ}/{video_id}.json"
        tasks.append(
            lambda url=url, name=addon.name: http_get_json(url, what=f"sottotitoli ({name})").get(
                "subtitles", []
            )
        )
    return _dedup(_gather(tasks), lambda s: s.get("url") or s.get("id") or id(s))
