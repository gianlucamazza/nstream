# 0002. RealDebrid: stay on Torrentio, no native cache path

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** project maintainer
- **Implemented in:** `debrid.get_resolver` returns `None` for realdebrid (no resolver)

## Context

RealDebrid is the most common debrid provider and the reference case for the adapter layer
(ADR [0001](0001-native-debrid-resolver-adapter-layer.md)). Its REST API
(`https://api.real-debrid.com/rest/1.0/`, Bearer auth, 250 req/min) still exposes the
resolution flow: `POST /torrents/addMagnet` → `POST /torrents/selectFiles/{id}` →
`GET /torrents/info/{id}` (wait until `status=downloaded`) → `POST /unrestrict/link`.

The decisive fact: **RealDebrid disabled `/torrents/instantAvailability` in November 2024**
as an anti-piracy measure. There is no longer any batch "is this hash cached?" endpoint — the
exact call every Stremio addon (and the `[RD+]` marker) relied on. The only way to learn if a
hash is cached natively is to add the magnet and poll its status, which mutates account state
and burns rate-limit budget per hash.

## Decision

Do **not** build a native cache-check for RealDebrid. RD stays resolved **via Torrentio**
(Torrentio retains its own cache heuristic and still emits `[RD+]`). The `DebridResolver`
RealDebrid implementation, if added later, returns `cached() == {}` (unsupported) and resolves
only on explicit user action (add→poll→`unrestrict`) — never as a speculative batch.

## Rationale

| Approach | Why not |
|----------|---------|
| Native add-then-poll as cache check | One add + several `info` polls **per hash**, against a 250/min cap, for ~150 streams — infeasible and account-mutating. |
| Native `unrestrict` on the auto-pick only | Possible, but adds a slow add→poll→unrestrict on the hot path for no cache *discovery* gain, since Torrentio already supplies a ready url for cached RD streams. |
| **Keep RD on Torrentio (chosen)** | Torrentio still does RD resolution well; the `url_playable` + P2P fallback (`stream_select.py:234-259`) already covers Torrentio's stale-marker case. Native RD adds latency, quota risk, and account writes with no cache-check upside. |

This is why RealDebrid is **not** one of the "two integrations": its native value proposition
(cache discovery) was removed upstream. TorBox and Premiumize (ADR 0003/0004) keep a live
cache check and are the integrations worth building.

## Consequences

- No RealDebrid code on the hot path; the provider-agnostic Torrentio route is unchanged for RD.
- If a future RD endpoint restores batch availability, supersede this ADR rather than editing it.
- The adapter `Protocol`'s optional `cached()` (returns `{}`) is validated by this case: RD is
  the provider that legitimately has no cache check, proving the interface need not assume one.

## References

- [Real-Debrid API](https://api.real-debrid.com/) · [torrents/unrestrict endpoints](https://valentingot.github.io/real-debrid/available_requests/torrents.html)
- instantAvailability removal (Nov 2024): [ElfHosted — Stremio after RealDebrid](https://store.elfhosted.com/blog/2024/11/22/stremio-after-realdebrid/)
- Existing fallback that covers RD's stale markers: `stream_select.py:234-259`, `api.py:61-74`
