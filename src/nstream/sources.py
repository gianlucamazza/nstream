"""Known Stremio stream-source presets and playable-stream helpers.

Torrentio remains the built-in discovery path (`addons._builtins`). Everything else is
opt-in via `cfg.addons` (full manifest URLs). Public instances of Comet / MediaFusion /
AIOStreams / … require a **user-generated** manifest (debrid token and filters live in
the path) — nstream never ships a hard-coded credential-bearing URL. Presets only guide
the settings UI: pick a source → open its configure page → paste the resulting
`…/manifest.json`.

Leaf-ish: no imports from nstream runtime modules (stdlib only) so config/settings/api
can share the catalog without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePreset:
    """A well-known stream (or multi-resource) addon the user can wire in by URL."""

    id: str
    name: str
    configure_url: str
    blurb: str
    # What the addon typically contributes (display only; real capabilities come from
    # the fetched manifest).
    resources: tuple[str, ...] = ("stream",)


# Curated 2026 shortlist of VOD stream discovery alternatives to Torrentio.
# configure_url is the public instance's config UI; the user pastes the generated manifest.
STREAM_PRESETS: tuple[SourcePreset, ...] = (
    SourcePreset(
        id="comet",
        name="Comet",
        configure_url="https://comet.elfhosted.com/configure",
        blurb="Torrent + debrid; alternativa moderna e spesso più stabile a Torrentio.",
    ),
    SourcePreset(
        id="mediafusion",
        name="MediaFusion",
        configure_url="https://mediafusion.elfhosted.com/configure",
        blurb="Multi-source con filtri regionali e cataloghi extra.",
        resources=("stream", "catalog"),
    ),
    SourcePreset(
        id="aiostreams",
        name="AIOStreams",
        configure_url="https://aiostreams.elfhosted.com/stremio/configure",
        blurb="Meta-aggregatore multi-addon + debrid/Usenet in un solo manifest.",
    ),
    SourcePreset(
        id="torrentsdb",
        name="TorrentsDB",
        configure_url="https://torrentsdb.com/configure",
        blurb="Discovery torrent multi-indexer (esperienza simile a Torrentio).",
    ),
)

_PRESET_BY_ID: dict[str, SourcePreset] = {p.id: p for p in STREAM_PRESETS}


def preset(preset_id: str) -> SourcePreset | None:
    return _PRESET_BY_ID.get(preset_id)


def preset_by_name(name: str) -> SourcePreset | None:
    """Match a live addon name to a preset (case-insensitive), if any."""
    key = (name or "").casefold()
    for p in STREAM_PRESETS:
        if p.name.casefold() == key or p.id.casefold() == key:
            return p
    return None


def is_playable_stream(stream: object) -> bool:
    """True when nstream can resolve the stream to playback (HTTP url or torrent infoHash).

    Stremio also has `ytId` / `externalUrl` shapes and hoster rows that only open a browser —
    those are intentionally dropped: no in-player YouTube and no external hand-off.
    """
    if not isinstance(stream, dict):
        return False
    return bool(stream.get("url") or stream.get("infoHash"))
