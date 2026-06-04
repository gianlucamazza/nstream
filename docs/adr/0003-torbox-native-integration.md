# 0003. TorBox native resolver

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** project maintainer
- **Implemented in:** `debrid.TorBoxResolver`

## Context

TorBox became the most-recommended debrid provider after RealDebrid's November 2024
restrictions (ADR [0002](0002-realdebrid-native-integration.md)). Unlike RealDebrid it
**retains a live, batch cache check**, so a native resolver can verify "is this cached?"
truthfully instead of trusting Torrentio's marker — the core value of going native.

API (`https://api.torbox.app/v1/api`, Bearer auth):

- `GET /torrents/checkcached?hash=<h1,h2,…>` — batch cache check (the live signal RD lost).
- `POST /torrents/createtorrent` — add a magnet; `add_only_if_cached=true` adds only if cached.
  **Rate limited to 60/hour** per token (the rest of the API is 300/min).
- `GET /torrents/mylist` — poll torrent/file state.
- `GET /torrents/requestdl?token=…&torrent_id=…&file_id=…&redirect=true` — a **permalink**.
  TorBox explicitly says to use this rather than saving CDN urls, which are not permanent.

Responses carry a `success` boolean plus status codes (200 ok, 400 bad input, 403 auth,
500 server).

## Decision

Implement a `TorBoxResolver` for the adapter layer (ADR 0001):

- `cached(hashes)` → batch `GET /torrents/checkcached`, returning the cached subset. This feeds
  the native `[TB+]` signal without Torrentio.
- `resolve(info_hash, file_idx)` → `createtorrent` (with `add_only_if_cached` when we only want
  instant playback), then resolve the file to a **`requestdl` permalink** (`redirect=true`),
  returned as the `http://…` url the rest of nstream expects.

Honour the **60/hour `createtorrent` cap**: batch-check first, only `createtorrent` for the
chosen stream (lazy, on the hot path), never per-menu-row. Reuse `api.py` retry/backoff; treat
the `success` flag + status codes as the error contract; back off on 429/500. Failures raise
`DebridUnavailable` → degrade to Torrentio/P2P.

## Rationale

TorBox is one of the two integrations that genuinely beat the Torrentio route: a live cache
check means the `cached` score term (`quality.py`, top precedence) reflects reality, removing
the stale-marker miss that v1.9's `url_playable` fallback exists to paper over. The permalink
(`requestdl`) sidesteps ephemeral-CDN-link expiry by construction, so nstream can resolve
lazily and not worry about link lifetime — a cleaner fit than RD's expiring `unrestrict` link.

The 60/hour `createtorrent` limit is the only real constraint, and it only bites if we add
uncached torrents aggressively; the cache-check-first + add-only-chosen flow keeps adds rare.

## Consequences

- New `TorBoxResolver` in `debrid.py` + tests; marker `TB`, reused by `quality`'s existing
  `[XX+]` consumer with no change to the regex (`quality.py:112`).
- Token stored per-provider (Bearer header) per ADR 0001; `log.py` redaction must scrub it.
- A quota-budget mindset on `createtorrent` (60/h): the resolver must cache-check before adding
  and only add the chosen stream — document this so future edits don't add a per-row add.
- Permalinks remove link-expiry handling for TorBox; the `url_playable` fallback still applies
  generically and harmlessly.

## References

- [TorBox Main API docs](https://api-docs.torbox.app/) · [Postman collection](https://www.postman.com/torbox/torbox-api/documentation/b6l9hbv/main-api)
- ADR [0001](0001-native-debrid-resolver-adapter-layer.md) (adapter contract); url-contract precedent `engine.py:238-254`
