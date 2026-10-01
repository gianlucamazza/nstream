"""Debrid provider keys — a leaf (stdlib only), so the bottom-tier `log` can build its
redaction regex from them without importing `config`."""

from __future__ import annotations

# Debrid provider keys Torrentio understands, embedded in `torrentio_base` as `key=token`.
# Single source of truth shared by settings (provider picker) and addons (strip for local
# backend); kept provider-agnostic — code never special-cases an individual provider.
# Single source of truth (key → display name); both the provider key set and the settings
# picker derive from it, and log.py builds its redaction regex from the keys — no drift.
DEBRID_PROVIDER_NAMES: dict[str, str] = {
    "realdebrid": "RealDebrid",
    "alldebrid": "AllDebrid",
    "premiumize": "Premiumize",
    "torbox": "TorBox",
    "debridlink": "Debrid-Link",
    "easydebrid": "EasyDebrid",
    "offcloud": "Offcloud",
    "putio": "Put.io",
}
DEBRID_PROVIDERS: tuple[str, ...] = tuple(DEBRID_PROVIDER_NAMES)
