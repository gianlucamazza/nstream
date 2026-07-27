# 0024. Multi-source stream discovery beyond Torrentio

- **Status:** Accepted
- **Date:** 2026-07-27
- **Deciders:** project maintainer
- **Implemented in:** `src/nstream/sources.py`, `addons.py`, `api.py`, `config.py`,
  `settings.py`, `labels.py`, `stream_select.py`, `headless.py`

## Context

nstream's stream discovery was a hard dependency on the built-in **Torrentio** addon
(`addons._builtins` always registered it; debrid token lived only in `torrentio_base`).
Outages, rate limits, and the desire to add Comet / MediaFusion / AIOStreams (or a pure
user-generated manifest) required a first-class multi-source path without abandoning
Torrentio or hard-coding credential-bearing public URLs.

Stream results from different addons often describe the **same release** (same
`behaviorHints.filename`) once as a ready debrid `url` and once as pure `infoHash`. The
hybrid `auto` backend already fused Torrentio's tokened + token-less queries; extra addons
did not participate in that fuse or in release-level collapse.

## Decision

1. **Torrentio is optional** (`config.torrentio_enabled`, default `true`). When off, only
   `cfg.addons` (and no other stream builtin) supply streams.
2. **Curated presets** live in `sources.STREAM_PRESETS` (Comet, MediaFusion, AIOStreams,
   TorrentsDB): configure-page URLs only — the user pastes a **generated** `…/manifest.json`
   into `cfg.addons`. nstream never ships a token in a URL.
3. **`api.streams`** fans out over every effective stream addon, stamps `stream["addon"]`
   for provenance, drops unplayable shapes (`ytId` / `externalUrl` only via
   `sources.is_playable_stream`), fuses url↔infoHash by filename
   (`_fuse_url_and_torrent`), then **collapses** same-filename rows across addons
   (`_dedup_by_release`: cached > url > pure torrent).
4. **Zero stream sources** → clear TUI/JSON (`no_stream_sources` +
   `sources.no_stream_source_message`) instead of a generic empty list.
5. Settings **Fonti stream / plugin** toggles Torrentio, adds from preset or custom URL,
   removes extras.

## Rationale

| Option | Verdict |
|--------|---------|
| Keep Torrentio-only | Simplest; remains SPOF for discovery. |
| Hard-code public Comet/MF instances | Tokens/rate limits change; violates "no secret in repo". |
| **Optional Torrentio + user manifests + fuse/dedup (chosen)** | Protocol-native, provider-agnostic ranking unchanged, multi-addon usable without a marketplace. |
| Replace Torrentio with AIOStreams built-in | Out of scope; user can already add AIO via `addons`. |

Multi-debrid (N tokens in one config, parallel Torrentio queries) is **out of this ADR** —
today `debrid_credentials` still reads a single `provider=token` segment. Options
(multi-Torrentio query, AIOStreams meta-debrid, native multi-resolver) need a separate
decision when a second provider is available.

## Consequences

- `Stream.addon` is set by `api.streams` and shown in `labels.stream_label` / `--explain`.
- Ranking (`quality`) stays marker-agnostic (`[RD+]`, `[TB+]`, …); provenance is display-only.
- With `torrentio_enabled: false` and empty `addons`, search/meta still work; play fails early
  with `no_stream_sources`.
- Maintenance: preset configure URLs may move; only labels in `sources.py` need updating.
- Local config may list explicit defaults (`torrentio_enabled`, cast_*); behaviour unchanged
  when keys were previously implicit.

## References

- ADR 0001 (native debrid resolver — resolve path, not discovery)
- ADR 0014 (verified cache pre-ranking — still applies to ready urls)
- `docs/selection.md` (ranking terms)
- Commits: multi-source discovery; harden (guard, provenance, dedup)
