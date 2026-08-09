# 0027. Per-addon circuit breaker on stream sources

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** project maintainer

## Context

ADR 0025 remembers **dead sources** (a release that answers 404/410) in a persistent negative
cache. There is no equivalent one level up: an **unreachable addon** is queried again on every
gather of every run.

Field measure, 2026-08-01 (nstream’s own HTTP client, not curl), Torrentio origin down
(Cloudflare HTTP 522):

| source | latency | outcome |
| ------ | ------- | ------- |
| comet.elfhosted.com | 0.1s | ok |
| torrentsdb.com | 0.1s | ok |
| other catalogs | ~0s | ok |
| **torrentio.strem.fun (built-in)** | **84.1s** | `NetworkError` after 4 attempts |

End-to-end `nstream --json --explain`: **~1m49s**, while Comet already had a complete answer
in under two seconds. `api` gather budget (`_GATHER_BUDGET`) caps how long a blocked source
is waited on, but does not prevent repaying that wait on every gather and every process
(search, catalog, streams — multiple gathers per run).

### What this is not

Retry policy is already correct and must not be weakened: `net.http_get_json` uses exponential
backoff with jitter (`util.backoff`), honours `Retry-After` on 429, and does not retry most
4xx. The defect is **memory**: every CLI process starts assuming every addon is healthy.

### Constraint that shapes the solution

Client `TIMEOUT` with retries means Cloudflare’s ~60s 522 is often **never observed** —
timeouts win first. Measured 84.1s ÷ 4 ≈ 21s per attempt. A breaker driven only by HTTP status
codes would be dead code on the incident that motivated this ADR. The available signal is
**repeated timeout / retryable failure**, not a clean status.

## Decision

### 1. One breaker per addon, not global

State keyed by addon base URL (built-in Torrentio included), persisted under `state/`. A
shared global breaker would block live Comet when Torrentio is dead (resource
differentiation).

### 2. Three-state machine

| state | behaviour |
| ----- | --------- |
| **Closed** | normal requests; failure counter with time window, self-resets |
| **Open** | source skipped **without network**; wait timer active |
| **Half-Open** | single probe request: success → Closed; failure → Open |

### 3. Trigger is repeated failure, not status alone

Open after a threshold of consecutive **retryable** failures, timeouts included. Accelerated
open on status remains only where status arrives inside the timeout (`503` / long
`Retry-After`), as a secondary path.

### 4. Half-Open probe is a real stream query

Not a `manifest.json` fetch (CDN cache can look healthy while origin is dead). Cost of a
wrong probe: one `TIMEOUT` per window, not per gather per run.

### 5. Timeouts from stream-query latency percentiles

Derive per-request timeout from observed **stream** query latency, not manifest latency
(healthy manifests ~0.1s; Comet streams ~1.3–2.2s). Too long blocks the thread before the
breaker can trip; too short marks a slow live source dead.

### 6. Visibility and manual override

Log every transition; `--explain` lists Open addons and since when. Silent skips would hide
why a title is missing. Manual reset flag (addon-level analogue of `--forget-dead`) returns
all breakers to Closed.

## Rationale

Retry expects eventual success; a circuit breaker **prevents** an operation that will likely
fail. Half-Open is what distinguishes this from a pure TTL skip: without a real re-admission
probe, reopen is either too early (repay the wait) or too late (blind to recovery).

## Consequences

- With a source Open, per-run latency returns to seconds; worst case no longer scales with
  the number of dead addons.
- Accepted risk: a slow but live source may open the breaker — mitigated by consecutive
  failure threshold, visibility, and manual override.
- New persisted state under `state/`; atomic write + best-effort (like ADR 0025) — a race
  loses at most a counter, never blocks playback.
- First access after timer expiry pays one full timeout if the source is still down.
- ADR 0025 remains valid and orthogonal (per-source vs per-addon).

**Implementation:** `state/breaker.py` (Closed / Open / Half-Open), wired through
`api._gather` with per-addon `keys`; streams / catalog / search / subtitles pass bases.
`--forget-breakers` clears state; `--explain` lists Open addons.

## References

- Azure Architecture Center — Circuit Breaker pattern:
  <https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker>
- `net.http_get_json`, `util.backoff`, `api` gather budget / `_gather`
- ADR 0024, ADR 0025
