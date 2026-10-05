"""Stremio addon-protocol client: built-in providers, user addons, resource dispatch.

A Stremio addon is identified by a ``…/manifest.json`` URL; its *base* is that URL minus
``/manifest.json`` and a resource is fetched at ``{base}/{resource}/{type}/{id}.json``.

The built-in providers (Cinemeta, Torrentio, OpenSubtitles) are described inline, so no
manifest fetch is needed for them; only user-added addons are fetched and cached on disk.
The cache is keyed by a hash of the manifest URL — a Torrentio URL embeds the Real-Debrid
token, so it must never be written to the cache file in clear.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import net, util
from .config import Config
from .providers import DEBRID_PROVIDERS

CACHE_TTL = 86400  # re-fetch a user addon's manifest at most once a day
# After a failed manifest fetch, don't retry it in this process for a while: every API call
# goes through `effective_addons`, so a down addon used to cost one timed-out fetch per call.
FAIL_TTL = 600.0
_failed_at: dict[str, float] = {}


@dataclass(frozen=True)
class Addon:
    base: str
    name: str
    resources: dict[str, dict]  # resource -> {"types": [...], "idPrefixes": [...]}
    # (type, id, display name) — name falls back to id when the manifest omits it.
    catalogs: tuple[tuple[str, str, str], ...] = ()
    builtin: bool = False
    # (type, id) of the catalogs that declare the `search` extra. Only these may be
    # queried with `search=`: an addon ignores the extra on any other catalog and answers
    # with unrelated rows (anime-kitsu returned "Hong Gil Dong 2084" for "Il grande Gatsby").
    search_catalogs: tuple[tuple[str, str], ...] = ()
    # (type, id, name) that the board may list: no required extra other than `skip`
    # (search-only catalogs stay off the menu — ADR 0046).
    board_catalogs: tuple[tuple[str, str, str], ...] = ()
    manifest_url: str = ""


# Cinemeta-shaped catalog ids already exposed as fixed TUI rows (Popolari / Novità / Top).
# User addons that re-declare these still contribute to the fetch fan-out; they just don't
# get a duplicate menu entry.
_BUILTIN_CATALOG_IDS = frozenset({"top", "year", "imdbRating"})
# Film / Serie TV section types (ADR 0009). A catalog whose type is outside this set
# (today: `anime`) is still offered on both sections.
_BOARD_TYPES = frozenset({"movie", "series"})
# Required extras the board already sends (`api._extras` skip=). Anything else (search,
# genre, …) keeps the catalog off `extra_catalogs` — genre/skip UI is a later slice.
_BOARD_REQUIRED_EXTRAS = frozenset({"skip"})


# --- manifest cache (user addons only) -----------------------------------


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "nstream" / "manifests.json"


def _load_cache() -> dict:
    return util.load_json(_cache_path(), {})


def _store_cache(cache: dict) -> None:
    # The cache is best-effort; never block playback on a write failure.
    with contextlib.suppress(OSError):
        util.atomic_write(
            _cache_path(),
            lambda f: json.dump(cache, f, ensure_ascii=False),
            prefix=".manifests-",
        )


# --- manifest parsing ----------------------------------------------------


def _base_of(manifest_url: str) -> str:
    suffix = "/manifest.json"
    if manifest_url.endswith(suffix):
        return manifest_url[: -len(suffix)]
    return manifest_url.rstrip("/")


def _declares_search(catalog: dict) -> bool:
    """True if a manifest catalog accepts the `search` extra (current `extra` list or the
    legacy `extraSupported` form)."""
    extra = catalog.get("extra")
    if isinstance(extra, list) and any(
        isinstance(e, dict) and e.get("name") == "search" for e in extra
    ):
        return True
    legacy = catalog.get("extraSupported")
    return isinstance(legacy, list) and "search" in legacy


def _required_extra_names(catalog: dict) -> tuple[str, ...]:
    """Names the addon marks required (`extra[].isRequired` or legacy `extraRequired`)."""
    names: list[str] = []
    extra = catalog.get("extra")
    if isinstance(extra, list):
        for e in extra:
            if isinstance(e, dict) and e.get("isRequired") and e.get("name"):
                names.append(str(e["name"]))
    legacy = catalog.get("extraRequired")
    if isinstance(legacy, list):
        names.extend(str(x) for x in legacy if x)
    return tuple(names)


def _board_ok(catalog: dict) -> bool:
    """True if the board can GET this catalog without extras we do not send."""
    return all(n in _BOARD_REQUIRED_EXTRAS for n in _required_extra_names(catalog))


def _parse_manifest(manifest_url: str, data: dict) -> Addon:
    if not isinstance(data, dict):  # guard against a corrupt/partial cache entry
        data = {}
    m_types = data.get("types", [])
    m_idp = data.get("idPrefixes", [])
    resources: dict[str, dict] = {}
    for r in data.get("resources", []):
        if isinstance(r, str):
            resources[r] = {"types": list(m_types), "idPrefixes": list(m_idp)}
        elif isinstance(r, dict) and r.get("name"):
            resources[r["name"]] = {
                "types": list(r.get("types", m_types)),
                "idPrefixes": list(r.get("idPrefixes", m_idp)),
            }
    catalogs: list[tuple[str, str, str]] = []
    search_catalogs: list[tuple[str, str]] = []
    board_catalogs: list[tuple[str, str, str]] = []
    for c in data.get("catalogs", []):
        if not isinstance(c, dict):
            continue
        cat_id = str(c.get("id") or "")
        if not cat_id:
            continue
        typ = str(c.get("type") or "")
        name = str(c.get("name") or cat_id).strip() or cat_id
        catalogs.append((typ, cat_id, name))
        if _declares_search(c):
            search_catalogs.append((typ, cat_id))
        if _board_ok(c):
            board_catalogs.append((typ, cat_id, name))
    base = _base_of(manifest_url)
    return Addon(
        base=base,
        name=data.get("name") or base,
        resources=resources,
        catalogs=tuple(catalogs),
        manifest_url=manifest_url,
        search_catalogs=tuple(search_catalogs),
        board_catalogs=tuple(board_catalogs),
    )


def load_addon(manifest_url: str, *, use_cache: bool = True) -> Addon | None:
    """Fetch+parse a user addon manifest (cached). Returns None if unreachable.

    A stale cached manifest is served immediately and refreshed on a background thread:
    a synchronous refresh sat on every cold run's critical path, outside any gather budget
    (~24s once a down addon's manifest passed its TTL). A failed refresh is remembered in
    the cache (`fail_ts`) so later processes don't retry it for FAIL_TTL."""
    key = hashlib.sha256(manifest_url.encode()).hexdigest()
    if not use_cache:
        return _fetch_manifest(manifest_url, key, {})
    cache = _load_cache()
    entry = cache.get(key)
    if entry and "manifest" in entry:
        fresh = (time.time() - entry.get("ts", 0)) < CACHE_TTL
        failed_recently = (time.time() - entry.get("fail_ts", 0)) < FAIL_TTL
        if not fresh and not failed_recently:
            _refresh_in_background(manifest_url, key)
        return _parse_manifest(manifest_url, entry["manifest"])
    if time.monotonic() - _failed_at.get(key, -FAIL_TTL) < FAIL_TTL:
        return None
    return _fetch_manifest(manifest_url, key, cache)


def _fetch_manifest(manifest_url: str, key: str, cache: dict) -> Addon | None:
    try:
        # One try only: a dead user addon must not stall the flow for ~60s.
        data = net.http_get_json(manifest_url, what="manifest addon", retries=1)
    except net.NetworkError:
        _failed_at[key] = time.monotonic()
        return None
    _failed_at.pop(key, None)
    cache[key] = {"ts": int(time.time()), "manifest": data}
    _store_cache(cache)
    return _parse_manifest(manifest_url, data)


_refreshing: set[str] = set()
_refresh_lock = threading.Lock()


def _refresh_in_background(manifest_url: str, key: str) -> None:
    """Best-effort refresh of a stale manifest; the process may exit before it lands."""
    with _refresh_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run() -> None:
        try:
            data = net.http_get_json(manifest_url, what="manifest addon", retries=1)
        except net.NetworkError:
            data = None
        cache = _load_cache()
        entry = dict(cache.get(key) or {})
        if data is None:
            entry["fail_ts"] = int(time.time())
        else:
            entry = {"ts": int(time.time()), "manifest": data}
        if "manifest" in entry:
            cache[key] = entry
            _store_cache(cache)
        with _refresh_lock:
            _refreshing.discard(key)

    threading.Thread(target=run, name="manifest-refresh", daemon=True).start()


# --- built-ins + dispatch ------------------------------------------------


def _strip_debrid(base: str) -> str:
    """Drop any debrid segment from a Torrentio config string, so Torrentio returns
    pure-torrent results (infoHash) instead of debrid urls."""
    segs = [s for s in base.split("|") if s and s.split("=", 1)[0] not in DEBRID_PROVIDERS]
    return "|".join(segs) or "sort=qualitysize"


def _torrentio_url(base: str) -> str:
    return "https://torrentio.strem.fun/" + urllib.parse.quote(base, safe="=|")


def torrentio_base(cfg: Config) -> str:
    base = cfg.torrentio_base
    # "local" streams torrents itself and "native" resolves the chosen one through the
    # provider's own API, so both query Torrentio token-less (pure-torrent infoHash). The
    # hybrid "auto" backend keeps the token here (debrid urls / cached) and fetches the
    # pure-torrent variant separately (see torrentio_token_less / api hybrid merge).
    if cfg.playback_backend in ("local", "native"):
        base = _strip_debrid(base)
    return _torrentio_url(base)


def torrentio_token_less(cfg: Config) -> str:
    """Torrentio base URL with the debrid segment stripped — pure-torrent results
    (infoHash). Used by the hybrid 'auto' backend's P2P-fallback query."""
    return _torrentio_url(_strip_debrid(cfg.torrentio_base))


def _builtins(cfg: Config) -> list[Addon]:
    out = [
        Addon(
            base=cfg.cinemeta.rstrip("/"),
            name="Cinemeta",
            builtin=True,
            resources={
                "catalog": {"types": ["movie", "series"], "idPrefixes": ["tt"]},
                "meta": {"types": ["movie", "series"], "idPrefixes": ["tt"]},
            },
            catalogs=tuple(
                (t, c, c) for c in ("top", "year", "imdbRating") for t in ("movie", "series")
            ),
            search_catalogs=(("movie", "top"), ("series", "top")),
            board_catalogs=tuple(
                (t, c, c) for c in ("top", "year", "imdbRating") for t in ("movie", "series")
            ),
        ),
    ]
    # Torrentio is optional: when disabled, stream discovery is only from cfg.addons
    # (Comet / MediaFusion / AIOStreams / custom). Meta + subs builtins stay.
    if cfg.torrentio_enabled:
        out.append(
            Addon(
                base=torrentio_base(cfg),
                name="Torrentio",
                builtin=True,
                resources={"stream": {"types": ["movie", "series"], "idPrefixes": ["tt", "kitsu"]}},
            )
        )
    out.append(
        Addon(
            base=cfg.opensubtitles.rstrip("/"),
            name="OpenSubtitles",
            builtin=True,
            resources={"subtitles": {"types": ["movie", "series"], "idPrefixes": []}},
        )
    )
    return out


def effective_addons(cfg: Config) -> list[Addon]:
    """Built-in providers plus the user's extra addons, deduped by base."""
    result = _builtins(cfg)
    for url in cfg.addons:
        addon = load_addon(url)
        if addon is not None:
            result.append(addon)
    seen: set[str] = set()
    out: list[Addon] = []
    for addon in result:
        if addon.base not in seen:
            seen.add(addon.base)
            out.append(addon)
    return out


def search_catalog(addon: Addon, typ: str) -> str | None:
    """Id of the first catalog of `typ` that `addon` declares searchable, else None."""
    return next((c for t, c in addon.search_catalogs if t == typ), None)


def has_catalog(addon: Addon, typ: str, cat: str) -> bool:
    """True if `addon` declares a catalog with this type and id."""
    return any(t == typ and c == cat for t, c, *_ in addon.catalogs)


def catalog_fetch_type(addon: Addon, typ: str, cat: str) -> str | None:
    """Path type for ``/catalog/{type}/{id}``. Prefer `typ` when the addon declares
    that pair; otherwise the addon's own type for `cat` (anime catalogs opened from
    a Film/Serie section — ADR 0046). None if this addon does not declare `cat`."""
    if has_catalog(addon, typ, cat):
        return typ
    return next((t for t, c, *_ in addon.catalogs if c == cat), None) or None


def extra_catalogs(cfg: Config, typ: str) -> list[tuple[str, str]]:
    """User-addon catalogs for a typed board section as `(catalog_id, display_label)`.

    Includes Cinemeta-shaped rows of `typ` and catalogs whose type is not a board
    type (they appear in both Film and Serie). Skips built-ins, pinned Cinemeta ids,
    and catalogs that require extras the board does not send. Dedupes by catalog id
    (first declaration wins). Labels are ``Addon · name`` so TMDB "Popular" is not
    confused with Cinemeta "Popolari".
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for addon in effective_addons(cfg):
        if addon.builtin:
            continue
        rows = addon.board_catalogs or addon.catalogs
        if not rows:
            continue
        for t, cat_id, name in rows:
            if not cat_id or cat_id in _BUILTIN_CATALOG_IDS or cat_id in seen:
                continue
            if t == typ:
                include = serves(addon, "catalog", typ)
            elif t in _BOARD_TYPES:
                include = False
            else:
                include = serves(addon, "catalog", typ) or bool(t and serves(addon, "catalog", t))
            if not include:
                continue
            seen.add(cat_id)
            shown = name.strip() if name else cat_id
            out.append((cat_id, f"{addon.name} · {shown}"))
    return out


def serves(addon: Addon, resource: str, typ: str, video_id: str | None = None) -> bool:
    """True if `addon` provides `resource` for this type (and id prefix, if given)."""
    spec = addon.resources.get(resource)
    if not spec:
        return False
    if spec["types"] and typ not in spec["types"]:
        return False
    prefixes = spec["idPrefixes"]
    return not (video_id and prefixes and not any(video_id.startswith(p) for p in prefixes))


def has_stream_source(cfg: Config) -> bool:
    """True when at least one effective addon can serve the `stream` resource.

    False means Torrentio is disabled and no stream-capable extra is configured —
    search/meta still work, but every title will return zero streams.
    """
    return any("stream" in a.resources for a in effective_addons(cfg))
