# 0010. Background Chromecast discovery with a verified disk cache

- **Status:** Accepted
- **Date:** 2026-06-11
- **Deciders:** project maintainer
- **Implemented in:** `src/nstream/discovery.py` (new) + `caster._discover`, startup kickoff in
  `cli._dispatch`, same day

## Context

Casting is optional, but device discovery used to block the UX on the critical path:
`caster.resolve_device()` called `settings.scan_devices()` synchronously — `catt scan`
with a 20 s timeout (`util.CATT_SCAN_TIMEOUT`), retried once on an empty result, so up to
**40 s of frozen TUI** at play time, with no spinner and no way to skip. When no device was
on the LAN (TV off, different network), "nessun Chromecast in rete" arrived only after the
full timeout, and only then did playback degrade to local mpv. Nothing was cached: every
play-with-cast, `--status` and `--stop` paid a fresh mDNS scan, which is also flaky on
multi-interface hosts (docker bridges, VPNs) — a scan can come back empty while the device
is alive and reachable.

## Decision

nstream discovers Chromecasts in the background and never blocks the interactive flow on a
scan. A leaf module `discovery.py` owns the `catt scan` primitive, a daemon-thread
singleton started at TUI startup (`cli._dispatch`), a TTL disk cache
(`$XDG_CACHE_HOME/nstream/devices.json`, 24 h) and a ~1 s TCP probe to the cast control
port 8009 (`verify`). `caster.resolve_device()` consults, in order: a cache-verified
target (instant), the background-scan result (short bounded wait — 6 s default, 25 s for
an explicit picker, Ctrl-C skips to local), and finally cached devices that still answer
(rescue when mDNS is flaky). Headless callers block until the scan completes
(deterministic), but get the same instant cache path.

## Rationale

Options considered:

| Option                                        | Trade-off                                                                                                                                                                       |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Keep sync scan, add a spinner                 | Honest, but still 20–40 s of dead time on the happy path                                                                                                                        |
| Scan on startup, foreground                   | Penalises every launch for a feature that may not be used                                                                                                                       |
| **Background scan + verified cache (chosen)** | Scan cost is paid while the user browses; cache makes the common case (same TV as yesterday) ~1 s across sessions; `verify()` keeps stale IPs from masquerading as live devices |
| Cache without verification                    | A DHCP-moved IP would be cast to blindly; verification is one cheap TCP connect                                                                                                 |

A live TCP connection to port 8009 is stronger evidence than an mDNS answer, so the cache
rescue also _fixes_ the flaky-scan case instead of merely tolerating it. An empty scan
never overwrites a populated cache (flakiness again); TTL + verification handle genuinely
stale entries. Ctrl-C as the skip mechanism needs no extra key handling: the wait is the
only blocking point, and `CastUnavailable` already routes to local playback.

## Consequences

- New leaf module `discovery.py` (stdlib only: `socket`/`threading`/`json`); `caster` no
  longer imports `settings` — the scan primitive moved out of the settings menu.
- Worst-case interactive block drops from ~40 s to ~6 s (skippable); cached single-TV
  homes resolve in ~1 s; headless `--status`/`--stop` get the same fast path.
- New cache file `devices.json` (best-effort atomic writes, like every nstream cache); a
  device list can now be up to 24 h stale, gated by per-use verification.
- One `catt scan` runs per interactive session even when the user never casts — accepted:
  it is invisible (output captured, daemon thread) and refreshes the cache.
- The settings device picker still scans synchronously (the user asked for a scan) and
  doubles as a manual cache refresh.

## References

- ADR 0005/0007 (cast tiers, castbridge) — device resolution feeds both.
- `src/nstream/discovery.py`, `src/nstream/caster.py` (`_discover`, wait budgets),
  `src/nstream/cli.py` (`_dispatch` kickoff), `src/nstream/settings.py` (picker).
- Google Cast control port 8009 (castv2 TLS) as the liveness probe target.
