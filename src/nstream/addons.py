"""Stremio addon-protocol client: built-in providers, user addons, resource dispatch.

A Stremio addon is identified by a ``…/manifest.json`` URL; its *base* is that URL minus
``/manifest.json`` and a resource is fetched at ``{base}/{resource}/{type}/{id}.json``.

The built-in providers (Cinemeta, Torrentio, OpenSubtitles) are described inline, so no
manifest fetch is needed for them; only user-added addons are fetched and cached on disk.
The cache is keyed by a hash of the manifest URL — a Torrentio URL embeds the Real-Debrid
token, so it must never be written to the cache file in clear.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import api
from .config import Config

CACHE_TTL = 86400  # re-fetch a user addon's manifest at most once a day


@dataclass(frozen=True)
class Addon:
    base: str
    name: str
    resources: dict[str, dict]  # resource -> {"types": [...], "idPrefixes": [...]}
    catalogs: tuple[tuple[str, str], ...] = ()  # (type, id) pairs
    builtin: bool = False
    manifest_url: str = ""


# --- manifest cache (user addons only) -----------------------------------


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "nstream" / "manifests.json"


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_cache(cache: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".manifests-", suffix=".tmp", dir=path.parent)
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass  # the cache is best-effort; never block playback on it


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
    catalogs = tuple((c.get("type", ""), c.get("id", "")) for c in data.get("catalogs", []))
    base = _base_of(manifest_url)
    return Addon(
        base=base,
        name=data.get("name") or base,
        resources=resources,
        catalogs=catalogs,
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
        data = api.http_get_json(manifest_url, what="manifest addon", retries=1)
    except api.NetworkError:
        # Fall back to a stale copy rather than dropping the addon entirely.
        return _parse_manifest(manifest_url, entry["manifest"]) if entry else None
    cache[key] = {"ts": int(time.time()), "manifest": data}
    _store_cache(cache)
    return _parse_manifest(manifest_url, data)


# --- built-ins + dispatch ------------------------------------------------


def torrentio_base(cfg: Config) -> str:
    return "https://torrentio.strem.fun/" + urllib.parse.quote(cfg.torrentio_base, safe="=|")


def _builtins(cfg: Config) -> list[Addon]:
    return [
        Addon(
            base=cfg.cinemeta.rstrip("/"),
            name="Cinemeta",
            builtin=True,
            resources={
                "catalog": {"types": ["movie", "series"], "idPrefixes": ["tt"]},
                "meta": {"types": ["movie", "series"], "idPrefixes": ["tt"]},
            },
            catalogs=tuple(
                (t, c) for c in ("top", "year", "imdbRating") for t in ("movie", "series")
            ),
        ),
        Addon(
            base=torrentio_base(cfg),
            name="Torrentio",
            builtin=True,
            resources={"stream": {"types": ["movie", "series"], "idPrefixes": ["tt", "kitsu"]}},
        ),
        Addon(
            base=cfg.opensubtitles.rstrip("/"),
            name="OpenSubtitles",
            builtin=True,
            resources={"subtitles": {"types": ["movie", "series"], "idPrefixes": []}},
        ),
    ]


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


def serves(addon: Addon, resource: str, typ: str, video_id: str | None = None) -> bool:
    """True if `addon` provides `resource` for this type (and id prefix, if given)."""
    spec = addon.resources.get(resource)
    if not spec:
        return False
    if spec["types"] and typ not in spec["types"]:
        return False
    prefixes = spec["idPrefixes"]
    return not (video_id and prefixes and not any(video_id.startswith(p) for p in prefixes))
