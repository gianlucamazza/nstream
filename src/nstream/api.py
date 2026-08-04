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
import re
import threading
import time
import unicodedata
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from . import addons, log, sources, util
from .config import Config

# HTTP-JSON primitives live in `net` (below both api and addons) to break the addons↔api
# cycle. Re-exported here so existing `api.http_get_json` / `api.NetworkError` / `api.url_playable`
# / `api.UA` / `api.TIMEOUT` references (and their monkeypatching in tests) keep working.
from .net import TIMEOUT, UA, NetworkError, http_get_json, url_playable
from .types import Meta, Stream, Subtitle, Video

_log = log.get_logger("api")

__all__ = ["TIMEOUT", "UA", "NetworkError", "http_get_json", "url_playable"]

# Same family as quality._CACHED_RE (keep the two in sync) — kept local so api stays above
# quality in the graph. Matches both addon dialects for a cached debrid row: [RD+] and [RD⚡].
_CACHED_NAME_RE = re.compile("\\[[A-Za-z]{2,6}[+\u26a1]\ufe0f?\\]")

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


# Cinemeta `runtime` is free text: "136 min", "55 min", "1h 30min", "2 h", "1:30".
_RUNTIME_H_RE = re.compile(r"(\d+)\s*h", re.I)
_RUNTIME_M_RE = re.compile(r"(\d+)\s*m", re.I)


def parse_runtime_s(raw: str) -> float:
    """Cinemeta's free-text `runtime` in seconds; 0.0 when absent or unparseable.
    Pure text, no I/O — the unit-testable half of `expected_runtime_s`."""
    text = raw or ""
    hours = _RUNTIME_H_RE.search(text)
    minutes = _RUNTIME_M_RE.search(text)
    if not hours and not minutes:
        return 0.0
    h = int(hours.group(1)) if hours else 0
    m = int(minutes.group(1)) if minutes else 0
    return float(h * 3600 + m * 60)


def expected_runtime_s(cfg: Config, typ: str, video_id: str) -> float:
    """Expected playtime of ONE video in seconds; 0.0 when unknown (callers then skip the
    duration vetting, ADR 0028 — an unknown runtime is never replaced by a default).

    For a series the runtime lives on the SERIES meta and is the length of the typical
    EPISODE: Cinemeta's `videos` entries carry none (see `episodes`), so the series id is
    taken from the episode id (`tt5675620:1:1` → `tt5675620`). Reads the disk-cached meta
    (token-free, TTL 600 s, usually already warm from the preview pane)."""
    meta_id = video_id.split(":", 1)[0] if typ == "series" else video_id
    if not meta_id:
        return 0.0
    obj = meta_cached_disk(cfg, typ, meta_id)
    return parse_runtime_s(str(obj.get("runtime") or ""))


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
    # NB: a stream addon's base may embed a debrid token — `what` uses the
    # addon name, never the URL, so errors never leak it. NOT cached (availability
    # changes between calls, and the URLs may carry the token).
    # Fan-out covers every effective stream addon (Torrentio if enabled + cfg.addons
    # presets like Comet/MediaFusion/AIOStreams). Unplayable shapes (ytId /
    # externalUrl only) are dropped; ready-url and pure-torrent rows are fused by
    # filename so a debrid hit can fall back to local P2P (any source, not only
    # Torrentio).
    tasks: list[Callable[[], list]] = []
    for addon in addons.effective_addons(cfg):
        if not addons.serves(addon, "stream", typ, video_id):
            continue
        url = f"{addon.base}/stream/{typ}/{video_id}.json"
        tasks.append(
            lambda url=url, name=addon.name: _stamp_addon(
                http_get_json(url, what=f"stream ({name})").get("streams", []),
                name,
            )
        )
    # Hybrid "auto" backend: the main Torrentio query carries the debrid token
    # (cached urls); also fetch the token-less variant (pure-torrent infoHash) so
    # a release can play via debrid AND fall back to local P2P. Only when Torrentio
    # is enabled — otherwise multi-addon fuse below still pairs url↔infoHash.
    if cfg.playback_backend == "auto" and cfg.torrentio_enabled:
        tl = f"{addons.torrentio_token_less(cfg)}/stream/{typ}/{video_id}.json"
        tasks.append(
            lambda tl=tl: _tagged(
                _stamp_addon(
                    http_get_json(tl, what="stream (Torrentio P2P)").get("streams", []),
                    "Torrentio",
                )
            )
        )
    if not tasks:
        return []  # no stream source configured (Torrentio off + empty addons)
    gathered = _gather(tasks)
    playable = [s for s in gathered if sources.is_playable_stream(s)]
    fused = _fuse_url_and_torrent(playable)
    # Collapse the same release seen on multiple addons (filename join), keeping the
    # best row (cached > url > pure torrent) while enriching the winner with any
    # missing infoHash from the losers.
    collapsed = _dedup_by_release(fused)
    return _dedup(collapsed, _stream_key)


# Marker key (private, stripped before returning) tagging the pure-torrent batch in the
# hybrid merge so it can be told apart from the debrid batch after the concurrent gather.
_P2P_TAG = "__p2p__"


def _tagged(streams: list[Stream]) -> list[Stream]:
    # _P2P_TAG is a transient, non-schema marker; operate via a plain-dict view so the
    # TypedDict stays type-clean (the key is stripped again in _fuse_url_and_torrent).
    for s in streams:
        cast("dict", s)[_P2P_TAG] = True
    return streams


def _stamp_addon(streams: list, addon_name: str) -> list[Stream]:
    """Tag each stream dict with its source addon name (provenance for labels/explain)."""
    out: list[Stream] = []
    for s in streams:
        if isinstance(s, dict):
            s["addon"] = addon_name
            out.append(cast("Stream", s))
    return out


def _filename(s: Stream) -> str:
    """Join key identifying one release across debrid/pure-torrent queries and addons.

    Same precedence as `quality._release_name` (ADR 0026) — `behaviorHints.filename`, then
    the `description` headline, then the deprecated `title` headline — and normalized through
    `util.release_key` so a container extension or a case difference can't split one release
    into two rows. Both dedups now key on the same value.
    """
    hints = s.get("behaviorHints") or {}
    name = (
        (hints.get("filename") or "")
        or (s.get("description") or "").split("\n", 1)[0].strip()
        or (s.get("title") or "").split("\n", 1)[0].strip()
    )
    return util.release_key(name)


def _copy_torrent_identity(dst: Stream, src: Stream) -> None:
    """Copy infoHash/fileIdx/sources onto a ready-url stream for P2P fallback."""
    if "infoHash" in src and "infoHash" not in dst:
        dst["infoHash"] = src["infoHash"]
    if "fileIdx" in src and "fileIdx" not in dst:
        dst["fileIdx"] = src["fileIdx"]
    if "sources" in src and "sources" not in dst:
        dst["sources"] = src["sources"]


def _fuse_url_and_torrent(streams: list[Stream]) -> list[Stream]:
    """Fuse ready-url rows with pure-torrent siblings across *all* stream addons.

    Match key: `behaviorHints.filename` (else title first line). A matched release
    keeps the debrid/HTTP `url` and gains `infoHash`/`fileIdx`/`sources` so playback
    can fall back to local P2P. Unmatched pure-torrent rows stay; internal `_P2P_TAG`
    is stripped. Same-infoHash pure rows already covered by a ready stream are dropped.
    """
    ready: list[Stream] = []
    pure: list[Stream] = []
    for s in streams:
        cast("dict", s).pop(_P2P_TAG, None)
        if s.get("url"):
            ready.append(s)
        elif s.get("infoHash"):
            pure.append(s)

    by_name: dict[str, Stream] = {}
    for t in pure:
        fn = _filename(t)
        if fn and fn not in by_name:
            by_name[fn] = t

    used_names: set[str] = set()
    out: list[Stream] = []
    for s in ready:
        fn = _filename(s)
        t = by_name.get(fn) if fn else None
        if t is not None:
            used_names.add(fn)
            _copy_torrent_identity(s, t)
        out.append(s)

    ready_hashes = {(s.get("infoHash"), s.get("fileIdx")) for s in out if s.get("infoHash")}
    for t in pure:
        fn = _filename(t)
        if fn and fn in used_names:
            continue
        if (t.get("infoHash"), t.get("fileIdx")) in ready_hashes:
            continue
        out.append(t)
    return out


def _merge_hybrid(debrid: list[Stream], torrents: list[Stream]) -> list[Stream]:
    """Backward-compatible wrapper: fuse tagged Torrentio debrid + P2P batches.

    Prefer `_fuse_url_and_torrent` for new call sites; kept for tests and clarity of
    the auto-backend Torrentio split.
    """
    return _fuse_url_and_torrent([*debrid, *torrents])


def _release_rank(s: Stream) -> tuple[int, int, int]:
    """Preference for cross-addon release collapse: cached marker > has url > has infoHash."""
    cached = 1 if _CACHED_NAME_RE.search(s.get("name") or "") else 0
    has_url = 1 if s.get("url") else 0
    has_hash = 1 if s.get("infoHash") else 0
    return (cached, has_url, has_hash)


def _dedup_by_release(streams: list[Stream]) -> list[Stream]:
    """Collapse rows that share the same release filename across addons.

    Keeps the higher `_release_rank` row; copies torrent identity onto the winner when
    the loser has an infoHash the winner lacks. Streams without a filename key pass
    through unchanged (still subject to url/infoHash dedup).
    """
    best: dict[str, Stream] = {}
    order: list[str] = []
    passthrough: list[Stream] = []
    for s in streams:
        fn = _filename(s)
        if not fn:
            passthrough.append(s)
            continue
        prev = best.get(fn)
        if prev is None:
            best[fn] = s
            order.append(fn)
            continue
        if _release_rank(s) > _release_rank(prev):
            _copy_torrent_identity(s, prev)
            # Keep the richer addon label when the winner lacked one.
            if not s.get("addon") and prev.get("addon"):
                s["addon"] = prev["addon"]
            best[fn] = s
        else:
            _copy_torrent_identity(prev, s)
            if not prev.get("addon") and s.get("addon"):
                # Prefer showing both when they differ? Keep the winner's; if empty, take loser.
                prev["addon"] = s["addon"]
    return [best[fn] for fn in order] + passthrough


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
