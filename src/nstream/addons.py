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
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import net, util
from .config import DEBRID_PROVIDERS, Config

CACHE_TTL = 86400  # re-fetch a user addon's manifest at most once a day


@dataclass(frozen=True)
class Addon:
    base: str
    name: str
    resources: dict[str, dict]  # resource -> {"types": [...], "idPrefixes": [...]}
    # (type, id, display name) — name falls back to id when the manifest omits it.
    catalogs: tuple[tuple[str, str, str], ...] = ()
    builtin: bool = False
    manifest_url: str = ""


# Cinemeta-shaped catalog ids already exposed as fixed TUI rows (Popolari / Novità / Top).
# User addons that re-declare these still contribute to the fetch fan-out; they just don't
# get a duplicate menu entry.
_BUILTIN_CATALOG_IDS = frozenset({"top", "year", "imdbRating"})


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
    for c in data.get("catalogs", []):
        if not isinstance(c, dict):
            continue
        cat_id = str(c.get("id") or "")
        if not cat_id:
            continue
        typ = str(c.get("type") or "")
        name = str(c.get("name") or cat_id).strip() or cat_id
        catalogs.append((typ, cat_id, name))
    base = _base_of(manifest_url)
    return Addon(
        base=base,
        name=data.get("name") or base,
        resources=resources,
        catalogs=tuple(catalogs),
        manifest_url=manifest_url,
    )


def load_addon(manifest_url: str, *, use_cache: bool = True) -> Addon | None:
    """Fetch+parse a user addon manifest (cached). Returns None if unreachable."""
    key = hashlib.sha256(manifest_url.encode()).hexdigest()
    cache = _load_cache() if use_cache else {}
    entry = cache.get(key)
    if entry and (time.time() - entry.get("ts", 0)) < CACHE_TTL:
        return _parse_manifest(manifest_url, entry["manifest"])
    try:
        # One try only: a dead user addon must not stall the flow for ~60s.
        data = net.http_get_json(manifest_url, what="manifest addon", retries=1)
    except net.NetworkError:
        # Fall back to a stale copy rather than dropping the addon entirely.
        return _parse_manifest(manifest_url, entry["manifest"]) if entry else None
    cache[key] = {"ts": int(time.time()), "manifest": data}
    _store_cache(cache)
    return _parse_manifest(manifest_url, data)


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


def has_catalog(addon: Addon, typ: str, cat: str) -> bool:
    """True if `addon` declares a catalog with this type and id."""
    return any(t == typ and c == cat for t, c, *_ in addon.catalogs)


def extra_catalogs(cfg: Config, typ: str) -> list[tuple[str, str]]:
    """User-addon catalogs for `typ` as `(catalog_id, display_label)`, in addon order.

    Skips built-ins and the Cinemeta-shaped ids already pinned in the TUI section menu.
    Dedupes by catalog id (first declaration wins the label). Labels prefer the manifest
    name and fall back to ``Addon · id`` when the name is just the bare id.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for addon in effective_addons(cfg):
        if addon.builtin or not serves(addon, "catalog", typ):
            continue
        for t, cat_id, name in addon.catalogs:
            if t != typ or not cat_id or cat_id in _BUILTIN_CATALOG_IDS or cat_id in seen:
                continue
            seen.add(cat_id)
            label = name if name and name != cat_id else f"{addon.name} · {cat_id}"
            out.append((cat_id, label))
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
