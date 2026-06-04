# 0004. Premiumize native resolver

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** project maintainer
- **Implemented in:** `debrid.PremiumizeResolver`

## Context

Premiumize is the second integration (with TorBox, ADR
[0003](0003-torbox-native-integration.md)) that still offers a **live cache check**, so a
native resolver adds real value over the Torrentio route — unlike RealDebrid (ADR
[0002](0002-realdebrid-native-integration.md)).

API (`https://www.premiumize.me/api`, Bearer/API-key auth):

- `POST /cache/check` — batch cache lookup by hash. Best-effort: a hit is not guaranteed.
- `POST /transfer/directdl` — for a cached (or previously transferred) link, returns
  **instantly-downloadable urls** directly in the response — no separate poll step.
- `POST /transfer/create` — submit an uncached link for **asynchronous** cloud fetch; the file
  becomes available later (poll `/transfer/list`).

Premiumize **phased out its old API**; only the endpoints above are current. Rate limits are
not publicly documented, so a conservative call rate is required.

## Decision

Implement a `PremiumizeResolver` for the adapter layer (ADR 0001):

- `cached(hashes)` → batch `POST /cache/check`, returning the cached subset (treated as
  best-effort — a miss is not authoritative).
- `resolve(info_hash, file_idx)` → `POST /transfer/directdl`; pick the file by index and return
  its instant url as the `http://…` contract. If `directdl` reports not-cached, raise
  `DebridUnavailable` (degrade) rather than firing `transfer/create` — async cloud fetch can take
  minutes and is not a playback path.

Resolve **lazily** for the chosen stream only. Because rate limits are undocumented, be
conservative: batch the `cache/check`, reuse `api.py` backoff, and don't poll tightly. Parse
the `status: success|error` envelope as the error contract.

## Rationale

`directdl` collapses add→poll→link into one call for cached content — the simplest native
resolve of the three providers, and it returns the playable url synchronously. The cache check,
though best-effort, still lets the `cached` score term reflect Premiumize reality better than a
Torrentio guess.

`transfer/create` (async fetch of uncached content) is deliberately **out of scope**: nstream
is an interactive "play now" tool, and a multi-minute cloud download is better served by the
existing P2P/Torrentio path. Keeping it out avoids a long-running state machine in a leaf module.

## Consequences

- New `PremiumizeResolver` in `debrid.py` + tests; marker `PM`, consumed by the existing
  `[XX+]` handling (`quality.py:112`) unchanged.
- Per-provider token (Bearer) per ADR 0001; `log.py` redaction must scrub it.
- Cache check is best-effort by contract — callers must not treat a Premiumize cache miss as
  definitive (the generic `url_playable` fallback still guards playback).
- No async-transfer code: uncached Premiumize content falls through to Torrentio/P2P, keeping the
  resolver synchronous and the module a leaf.

## References

- [Premiumize API](https://www.premiumize.me/api) · [old-API phaseout & cache best practices](https://blog.premiumize.me/old-api-phaseout-new-api-changes-and-best-practices-for-the-cache/)
- ADR [0001](0001-native-debrid-resolver-adapter-layer.md) (adapter contract); url-contract precedent `engine.py:238-254`
