"""HTTP access to the Stremio addon APIs (Cinemeta, Torrentio) with retry/backoff.

All requests share a timeout and retry only transient failures (timeouts, connection
errors, HTTP 429/5xx) with exponential backoff, honouring ``Retry-After``. Client
errors (4xx) are not retried. Error messages never include the request URL, so the
Real-Debrid token embedded in Torrentio URLs is never leaked to logs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
import unicodedata
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from . import addons, log, util
from .config import Config, Meta, Stream, Subtitle, Video

# HTTP-JSON primitives live in `net` (below both api and addons) to break the addons↔api
# cycle. Re-exported here so existing `api.http_get_json` / `api.NetworkError` / `api.url_playable`
# / `api.UA` / `api.TIMEOUT` references (and their monkeypatching in tests) keep working.
from .net import TIMEOUT, UA, NetworkError, http_get_json, url_playable

_log = log.get_logger("api")

__all__ = ["TIMEOUT", "UA", "NetworkError", "http_get_json", "url_playable"]

_MAX_WORKERS = 8
# Overall deadline (seconds) for one concurrent gather. A single stuck addon can take
# ~80s alone (retries × per-request timeout); past this shared budget its future is
# dropped — same best-effort spirit as `_safe` with NetworkError — so one hung addon
# never holds the picker hostage.
_GATHER_BUDGET = 25.0
# In-process TTL cache for token-free metadata (search/catalog/meta/episodes). Each
# CLI run is a fresh process, but the home TUI / browse / pick loops live in one
# process, so this makes back-navigation and re-browse within a session instant.
# Stream/subtitle responses are NOT cached (RD availability changes; their URLs hold
# the token).
_META_TTL = 600.0
_meta_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _dedup(items: list, key) -> list:
    seen: set = set()
    out: list = []
    for it in items:
        k = key(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def _norm_text(value: object) -> str:
    """Accent/punctuation-insensitive text used only for local result ranking."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    # Keep separators between words: ``Spider-Man`` and ``Spider Man`` must
    # normalize to the same value. Combining marks are dropped after NFKD so
    # accented and unaccented text compare identically.
    chars = []
    for c in text:
        if c.isalnum():
            chars.append(c)
        elif c.isspace() or unicodedata.category(c).startswith("P"):
            chars.append(" ")
    return " ".join("".join(chars).split())


def _search_score(meta: Meta, query: str) -> tuple[int, int, int, int, str]:
    wanted = _norm_text(query)
    name = _norm_text(meta.get("name"))
    info = _norm_text(meta.get("releaseInfo"))
    exact = int(name == wanted)
    prefix = int(name.startswith(wanted) and bool(wanted))
    words = int(bool(wanted) and wanted in name)
    year = int(bool(info) and info in wanted)
    return (exact, prefix, words, year, name)


def _gather(tasks: list[Callable[[], list]]) -> list:
    """Run per-addon/per-type fetch tasks concurrently, flattening results in task
    order (so the built-in providers keep priority for dedup). A task raising
    NetworkError contributes nothing (best-effort aggregation), and the whole gather
    shares one `_GATHER_BUDGET` deadline: a future still pending past it is dropped
    (cancelled best-effort, empty result) so a stuck addon can't block the TUI. One
    task → run inline (no thread overhead)."""
    if not tasks:
        return []
    if len(tasks) == 1:
        results = [_safe(tasks[0])]
    else:
        deadline = time.monotonic() + _GATHER_BUDGET
        ex = ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tasks)))
        try:
            futures = [ex.submit(_safe, t) for t in tasks]
            results = []
            for f in futures:  # in submit order
                try:
                    results.append(f.result(timeout=max(0.0, deadline - time.monotonic())))
                except TimeoutError:
                    f.cancel()  # best-effort: a task already running can't be cancelled
                    _log.warning("addon oltre il budget di %.0fs → scartato", _GATHER_BUDGET)
                    results.append([])
        finally:
            # Never wait for stragglers: their threads end on their own (http_get_json
            # has a per-request timeout) and still-queued futures are cancelled.
            ex.shutdown(wait=False, cancel_futures=True)
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


def search(cfg: Config, query: str, typ: str | None = None) -> list[Meta]:
    q = urllib.parse.quote(query)
    tasks: list[Callable[[], list]] = []
    for t in (typ,) if typ else ("movie", "series"):
        tasks += _catalog_tasks(cfg, t, f"top/search={q}", f"ricerca {t}")
    results = _dedup(_gather(tasks), lambda m: m.get("id") or id(m))
    # Rank flags descending while keeping equal-score titles alphabetic and
    # deterministic, independent of addon response order.
    return sorted(
        results,
        key=lambda m: (
            -_search_score(m, query)[0],
            -_search_score(m, query)[1],
            -_search_score(m, query)[2],
            -_search_score(m, query)[3],
            _search_score(m, query)[4],
        ),
    )


def _catalog_addon_tasks(cfg: Config, typ: str, cat: str, extras: str) -> list[Callable[[], list]]:
    tasks: list[Callable[[], list]] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "catalog", typ):
            continue
        # Built-in Cinemeta has these catalogs; a user addon must declare them.
        if not addon.builtin and not addons.has_catalog(addon, typ, cat):
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


# --browse keyword → Cinemeta catalog id (the CLI's --browse choices).
CAT_MAP = {"popolari": "top", "nuovi": "year", "top": "imdbRating"}

# Cinemeta genre path segments (`/genre=Action`). Stable English tokens — the addon
# does not localise them. Used by the typed-section Generi… menu (ADR 0009 follow-up).
GENRES: tuple[str, ...] = (
    "Action",
    "Adventure",
    "Animation",
    "Biography",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "History",
    "Horror",
    "Music",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Sport",
    "Thriller",
    "War",
    "Western",
)

# Cinemeta serves ~100 metas per catalog page; a full page means "maybe more".
CATALOG_PAGE = 100


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
        _prune_meta_cache(path.parent)
    return fresh


def _prune_meta_cache(cache_dir: Path) -> None:
    """Best-effort: drop expired meta entries. The TTL is only checked on read, so files
    for titles never revisited would accumulate forever (posters and remuxes have their
    own GC; this closes the meta gap). Runs only on a cache-miss write — hits, the
    per-row hot path, pay nothing."""
    cutoff = time.time() - _META_TTL
    with contextlib.suppress(OSError):
        for p in cache_dir.iterdir():
            with contextlib.suppress(OSError):
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()


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
    # _P2P_TAG is a transient, non-schema marker; operate via a plain-dict view so the
    # TypedDict stays type-clean (the key is stripped again in _merge_hybrid).
    for s in streams:
        cast("dict", s)[_P2P_TAG] = True
    return streams


def _is_p2p(s: Stream) -> bool:
    return bool(cast("dict", s).get(_P2P_TAG))


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
            # Copy the torrent identity onto the matched debrid stream so it can fall back to
            # local P2P. Explicit keys (not a loop) keep the TypedDict access type-safe.
            if "infoHash" in t and "infoHash" not in s:
                s["infoHash"] = t["infoHash"]
            if "fileIdx" in t and "fileIdx" not in s:
                s["fileIdx"] = t["fileIdx"]
            if "sources" in t and "sources" not in s:
                s["sources"] = t["sources"]
        out.append(s)
    out.extend(by_name.values())  # pure-torrent releases with no debrid match
    for s in out:
        cast("dict", s).pop(_P2P_TAG, None)
    return out


def _stream_key(s: Stream) -> object:
    """Dedup key: ready url when present, else the torrent's (infoHash, fileIdx) so
    pure-torrent streams (no url) survive instead of collapsing onto one another."""
    if s.get("url"):
        return s["url"]
    if s.get("infoHash"):
        return (s["infoHash"], s.get("fileIdx"))
    return id(s)


def _hash_subtitles(url: str, name: str) -> list[Subtitle]:
    """One addon's videoHash query. Only entries whose match marker says MOVIEHASH
    (`m == "h"`) are tagged `hash_match`: when the hash has no associations the addon
    falls back to the full imdb set (`m == "i"` — verified live on opensubtitles-v3,
    97/97 "i" for a hash with no DB entry), and tagging those would fabricate the very
    sync guarantee ADR 0018 exists to make honest."""
    subs = http_get_json(url, what=f"sottotitoli hash ({name})").get("subtitles", [])
    for s in subs:
        if s.get("m") == "h":
            s["hash_match"] = True
    return subs


def subtitles(
    cfg: Config,
    typ: str,
    video_id: str,
    *,
    video_hash: str | None = None,
    video_size: int = 0,
    filename: str | None = None,
) -> list[Subtitle]:
    """Subtitle tracks for a video, aggregated across addons. With `video_hash` (OSHash of
    the resolved stream) each addon is ALSO queried with the `videoHash`/`videoSize`/
    `filename` extras (Stremio protocol): those results are exact-file matches, tagged
    `hash_match` and listed first (the dedup keeps the first occurrence)."""
    extra = ""
    if video_hash:
        params: dict[str, str] = {"videoHash": video_hash}
        if video_size:
            params["videoSize"] = str(video_size)
        if filename:
            params["filename"] = filename
        # quote (not quote_plus): the extra rides in a PATH segment, where '+' is a
        # literal plus for a spec-correct parser — spaces must be %20.
        extra = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    tasks: list[Callable[[], list]] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "subtitles", typ, video_id):
            continue
        if extra:
            hash_url = f"{addon.base}/subtitles/{typ}/{video_id}/{extra}.json"
            tasks.append(lambda url=hash_url, name=addon.name: _hash_subtitles(url, name))
        url = f"{addon.base}/subtitles/{typ}/{video_id}.json"
        tasks.append(
            lambda url=url, name=addon.name: http_get_json(url, what=f"sottotitoli ({name})").get(
                "subtitles", []
            )
        )
    return _dedup(_gather(tasks), lambda s: s.get("url") or s.get("id") or id(s))
