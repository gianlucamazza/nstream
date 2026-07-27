#!/usr/bin/env python3
"""Rebuild RD-backed stream addon URLs in ~/.config/nstream/config.json from torrentio_base.

Keeps catalog-only addons. Never prints the debrid token.
Usage:  python scripts/refresh-stream-addons.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nstream" / "config.json"

# Catalog-only extras (no secrets) — discovery surface beyond Cinemeta.
CATALOG_ADDONS = [
    "https://94c8cb9f702d-tmdb-addon.baby-beamup.club/manifest.json",
    "https://anime-kitsu.strem.fun/manifest.json",
    "https://1fe84bc728af-stremio-anime-catalogs.baby-beamup.club/manifest.json",
]


def _token_from_base(base: str) -> str:
    for seg in base.split("|"):
        if seg.startswith("realdebrid=") and len(seg) > len("realdebrid="):
            return seg.split("=", 1)[1]
    raise SystemExit("nstream: nessun token realdebrid in torrentio_base")


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _ok_manifest(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nstream"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        return data.get("name") or url
    except Exception:
        return None


def main() -> None:
    raw = json.loads(CONFIG.read_text())
    token = _token_from_base(raw.get("torrentio_base") or "")

    comet_cfg = {
        "cachedOnly": False,
        "removeTrash": True,
        "maxResultsPerResolution": 10,
        "debridService": "realdebrid",
        "debridApiKey": token,
        "debridServices": [{"service": "realdebrid", "apiKey": token}],
        "enableTorrent": True,
        "deduplicateStreams": True,
        "scrapeDebridAccountTorrents": True,
    }
    comet = f"https://comet.elfhosted.com/{_b64url(comet_cfg)}/manifest.json"
    tdb = f"https://torrentsdb.com/sort=qualitysize|realdebrid={token}/manifest.json"

    stream: list[str] = []
    for url, label in ((comet, "Comet"), (tdb, "TorrentsDB")):
        name = _ok_manifest(url)
        if name is None:
            print(f"warn: {label} manifest non raggiungibile — saltato", file=sys.stderr)
            continue
        stream.append(url)
        print(f"ok {label} → {name}")

    # Preserve unknown user extras that aren't our stream hosts / known catalogs
    prev = list(raw.get("addons") or [])
    keep: list[str] = []
    for u in prev:
        if "comet.elfhosted.com" in u or "torrentsdb.com" in u:
            continue
        if u in CATALOG_ADDONS:
            continue
        if re.search(r"mediafusion\.elfhosted\.com/[^/]+/manifest", u):
            continue
        keep.append(u)

    catalogs: list[str] = []
    for u in CATALOG_ADDONS:
        if _ok_manifest(u):
            catalogs.append(u)
            print(f"ok catalog → {u.split('//', 1)[-1][:50]}")
        else:
            print(f"warn: catalogo non raggiungibile — {u}", file=sys.stderr)

    raw["addons"] = stream + catalogs + keep
    raw["torrentio_enabled"] = raw.get("torrentio_enabled", True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG)
    os.chmod(CONFIG, 0o600)
    print(f"scritto {CONFIG} ({len(raw['addons'])} addon, mode 600)")


if __name__ == "__main__":
    main()
