# 0014. Verify cached availability before committing to a pick

- **Status:** Accepted
- **Date:** 2026-07-14
- **Deciders:** project maintainer

## Context

The `[RD+]`/`[TB+]`… "cached" marker Torrentio returns is a **crowdsourced guess**, not a
guarantee: a debrid provider's cache entry can be stale or evicted. Ranking treats `cached` as
the highest-precedence term (`quality.score_components`), so a stale-cached release wins the
auto-pick, and only afterwards does `stream_select._ensure_playable` probe the resolved URL and
fall back. Observed live (Project Hail Mary): _"la sorgente «cached» non risponde, ripiego…"_ →
the top 4K cached pick was dead, and the fallback landed on a Dolby release that forced a
multi-GB Tier-2 remux — a slow, expensive detour caused by trusting one unverified marker.

`_ensure_playable` (`src/nstream/stream_select.py:477`) already reachability-checks the chosen
URL and tries the next candidates, but it runs **after** the pick is committed and only walks
the list serially from the top — it can burn several resolves before finding a live one, and it
cannot re-order the ranking around what is actually available.

## Decision

Before committing the auto-pick, **verify the real availability of the top-N cached
candidates** (a bounded, concurrent HEAD/short-Range probe of the resolved URL) and treat a
failed probe as "not cached" for ranking — so a live 1080p AAC release outranks a dead 4K
cached one, and the pick nstream commits to is one it has confirmed responds. The verdict is
memoised per URL for the process (like the ffprobe memo) so the probe is paid once.

## Rationale

Alternatives considered:

1. **Status quo** (`_ensure_playable` after the fact) — correct but wasteful: it can cascade
   through several dead cached picks and, worse, land in a remux because the _fallback_ order
   isn't availability-aware.
2. **Trust the provider, no probe** — fastest, but the failure we just hit is exactly this.
3. **Probe-then-rank (chosen)** — a small, bounded network cost up front buys a pick that is
   both best-ranked _and_ live, and keeps the expensive remux path from being entered by
   accident. Modern best practice for consuming an untrusted upstream signal: verify on the
   live system, don't trust the label (kb: probe-on-live-schema).

Bounding matters: probe only the top-N (e.g. 3-5) cached candidates, concurrently, with a
tight timeout, so the latency is one round-trip, not N. Uncached candidates are not probed
(their seeder count already gates them).

## Consequences

- `stream_select` gains a pre-commit verification step feeding `quality.rank_streams` (or a
  post-rank re-order): a `cached` release that fails its probe is demoted to uncached-equivalent.
  `_ensure_playable` stays as the last-resort net for a URL that dies between probe and play.
- **New cost:** up to N concurrent HEAD/Range requests per auto-pick (bounded, memoised). On a
  fully-live list this is pure overhead — keep N small and skip the probe entirely in
  interactive mode, where the user picks.
- **New dependency on `net`:** a lightweight availability probe (reuse `net.url_playable`,
  `src/nstream/net.py`) run concurrently (ThreadPool, same pattern as `api`).
- Interacts with ADR 0013/0015: fewer accidental remuxes means the remux path fires only when a
  title genuinely has no live decodable release.
- **Testing:** unit tests with a fake probe (live/dead/timeout matrix) asserting the re-order,
  and that the probe is bounded and memoised.

## As built (2026-07-14)

Shipped as a **post-rank demotion**, not a bespoke re-order — the demoted candidate flows
through the existing ranking/explain pipeline with zero downstream special-casing, the exact
inverse of `_mark_native_cached`:

- `stream_select._verify_cached_availability(cfg, results, *, cast, title)` runs in
  `prepare_stream` **before** `pick_and_resolve`, gated on `auto` and a non-`local` backend.
  It ranks once (`_auto_candidates`), takes the top-N (`_VERIFY_CACHED_CAP = 5`) candidates that
  are both cached (`quality.parse_stream(s).cached`) and carry a ready url, probes them
  concurrently (`ThreadPoolExecutor`), and for each dead one calls `_demote_cached` — which
  strips the `[XX+]` marker via the shared `quality._CACHED_RE`. `parse_stream` re-keys on the
  new name, so the very next rank pass scores it uncached.
- The probe is `_probe_url`, a process-lifetime memo (`_PROBE_MEMO`) over `api.url_playable`.
  `_ensure_playable` was repointed onto the same memo, so a url verified live up front is never
  re-probed when the pick is confirmed — and `_ensure_playable` stays as the last-resort net for
  a url that dies between probe and play.
- Uncached candidates are never probed (seeder-gated already); the local backend is a no-op
  (engine urls are buffer-gated, not cached-marked). Interactive picks skip it entirely.
- Tests: `test_verify_cached_demotes_dead_keeps_live`, `_bounded_to_cap`, `_skips_uncached`,
  `_noop_local_backend`, `test_probe_url_memoizes` (+ an autouse fixture clearing the memo).

## References

- `src/nstream/stream_select.py` (`_verify_cached_availability`, `_probe_url`, `_demote_cached`,
  `_ensure_playable`, `prepare_stream`), `src/nstream/quality.py` (`_CACHED_RE`,
  `score_components`, `rank_streams`), `src/nstream/net.py` (`url_playable`), `src/nstream/api.py`
  (concurrent pattern).
- kb: probe-on-live-schema, measure-before-optimizing. Relates to ADR 0005 (avoids needless
  Tier-2), 0013.
