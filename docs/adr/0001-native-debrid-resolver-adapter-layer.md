# 0001. Native debrid resolver adapter layer (alongside Torrentio)

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** project maintainer
- **Implemented in:** `src/nstream/debrid.py` (+ wiring in config/addons/stream_select/settings/log)

## Context

Today nstream is **fully provider-agnostic via Torrentio**: the debrid token is embedded in
the Torrentio config string (`sort=qualitysize|{provider}={token}`, `addons.py:124-141`),
Torrentio contacts the provider, and nstream receives stream objects with a ready `url`
(cached) or only an `infoHash` (pure-torrent). nstream **never calls a provider API**.
Cached detection is one generic regex (`quality.py:112`, `\[[A-Za-z]{2,6}\+\]`); token
redaction is one generic regex (`log.py:21`); resolution is delegated entirely to Torrentio.
This is a deliberate, documented principle (CLAUDE.md: *"keep it provider-agnostic — don't
special-case RealDebrid"*).

The cost is a hard dependency on a single third party. Torrentio rate-limits and outages are
a single point of failure for every debrid user, and native provider features (batch
instant-availability, permalinks, async cloud fetch) are unreachable. v1.9 already added a
`url_playable` reachability check + P2P fallback (`stream_select.py:234-259`, `api.py:61-74`)
precisely because Torrentio's cached marker is an unverifiable guess.

A native integration — nstream talking to provider APIs directly — would remove that single
point of failure, but it breaks the provider-agnostic principle: each API differs in cache
semantics, add/poll flow, link lifetime, and rate limits (see ADR 0002–0004). The question
is *how* to admit per-provider code without eroding the architecture.

## Decision

Introduce a thin **resolver adapter layer**: a new leaf module `src/nstream/debrid.py`
exposing a `DebridResolver` `Protocol` with one implementation per provider. Resolvers run
**alongside** Torrentio, not as a replacement — a new `playback_backend` value (e.g.
`native`) routes stream resolution through the adapter; `local`/`debrid`/`auto` are unchanged.

```python
class DebridResolver(Protocol):
    name: str      # "torbox" | "premiumize"
    marker: str    # "TB" | "PM" — the [XX+] prefix, reused by quality
    def cached(self, hashes: Sequence[str]) -> set[str]: ...   # subset cached; {} if unsupported
    def resolve(self, stream: Stream) -> str: ...             # http url; raises DebridUnavailable
```

`resolve()` takes a `Stream` (so it can reuse `engine.magnet_from_stream` for the magnet) and
returns a plain `http://…` url — the **same contract** `engine.resolve()` honours
(`engine.py:238-254`). The orchestrator stays ignorant of which backend produced the url, so
`player` / `caster` are untouched and `stream_select` only adds a native-first branch in its
existing resolve helpers. Best-effort like `engine`: `DebridUnavailable` degrades (to P2P),
never crashes the picker. Cached marking happens once in `stream_select.prepare_stream`, which
prefixes cached releases' names with the provider's `[XX+]` so the existing `quality` cached
score term ranks them first with no downstream change.

## Rationale

| Option | Verdict |
|--------|---------|
| Keep Torrentio-only (status quo) | Simplest, but the single-point-of-failure and the dead RD cache path (ADR 0002) remain unaddressed. |
| Replace Torrentio with native clients | Loses Torrentio's stream *discovery* (the ~150-stream catalog). nstream still needs Torrentio for the magnet list; only *resolution* is worth taking native. |
| **Adapter layer alongside Torrentio (chosen)** | Torrentio keeps discovering streams; the adapter takes over resolution per chosen stream. Per-provider code is quarantined behind one Protocol; the rest of the graph sees the existing url contract. |

The adapter mirrors `engine.py`: a leaf below `cli`, importing only `config`/`engine`/`log` +
stdlib (`engine` purely for the shared magnet builder), best-effort, returning the same url
shape. This is the project's proven pattern for an external backend, so it adds no new
architectural concept.

`cached()` is optional by contract (returns `{}` when a provider has no live check) so the
asymmetry between providers — RealDebrid lost its check, TorBox/Premiumize keep theirs — is
expressed in the interface, not special-cased in callers. `quality` keeps consuming a single
`[XX+]` marker; the resolver merely supplies it natively when available.

## Consequences

- **New module** `debrid.py` (leaf, like `engine.py`) + tests `test_debrid.py`. Import-graph
  discipline preserved: it never imports `cli`.
- **Token stays in one place.** Native APIs authenticate with `Authorization: Bearer <token>`
  (header, not query) — modern best practice, token out of URLs/redirects. Rather than a second
  per-provider config field (which could desync), the token is read from the existing single
  source `torrentio_base` via `config.debrid_credentials()` and sent as a header by the resolver.
- **`log.py` redaction now covers Bearer headers and `token=` query params** (TorBox's
  `requestdl` permalink), not only `{provider}=token` in URLs (`log.py:21`). To avoid even that,
  the TorBox resolver resolves `requestdl` server-side and hands the player a plain CDN url.
- **Resolution gains a real latency/quota budget.** Native resolve is add→poll→link, subject
  to provider rate limits (250–300/min; TorBox `createtorrent` 60/h). Reuse `api.py`'s existing
  retry/backoff and resolve **lazily** right before playback (links are ephemeral) — never
  pre-resolve a whole menu.
- **Per-provider maintenance** is now a standing cost: three APIs that can change (RD already
  removed an endpoint). The Protocol bounds the blast radius to one file per provider.
- The provider-agnostic-via-Torrentio principle is **narrowed, not abandoned**: discovery stays
  agnostic; only the new opt-in `native` backend carries per-provider code, behind one interface.

## References

- ADR [0002](0002-realdebrid-native-integration.md) (RealDebrid), [0003](0003-torbox-native-integration.md) (TorBox), [0004](0004-premiumize-native-integration.md) (Premiumize)
- Existing url-contract precedent: `engine.py:238-254`; reachability/fallback: `stream_select.py:234-259`, `api.py:61-74`
- Provider/marker registry to reuse: `config.py:183-192`, `quality.py:109-112`, `log.py:21`
