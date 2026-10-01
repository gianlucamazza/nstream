# 0038. A dead source is keyed by what failed, never by a shared display name

- **Status:** Proposed
- **Date:** 2026-10-01
- **Deciders:** maintainer
- **Amends:** 0025 (key derivation only; the `gone`-only persistence rule stands)

## Context

ADR 0025 keys the denylist with `availability.source_key`: infoHash first, then
`file:<filename>`, then `name:<display name>`. Two consequences surfaced in the 2026-10-01
audit:

- **A debrid link 404 bans the whole torrent.** `api.streams` fuses a debrid url row with
  the torrent's infoHash, so a 404 on _that provider's link_ is stored under the infoHash.
  For 30 days it then hides the P2P fallback and every other provider of the same torrent,
  although only one provider's link was proven gone.
- **The `name:` fallback can ban unrelated releases.** The key is the addon display name,
  e.g. `"[RD+] Torrentio 4k"`, which many releases share. A single 404 then hides all of
  them.

## Decision

The denylist key names what the probe actually proved gone:

- **Debrid url probe:** `<provider>:<infoHash>:<fileIdx|filename>`. It bans that
  provider's link to that file only.
- **Pure-torrent probe** (engine or swarm): `<infoHash>`, as today.
- **No `name:` key.** A row with neither an infoHash nor a filename is never denylisted, the
  same rule ADR 0025 already applies to rows with no key at all.

`prune_dead` matches a row against the key its own probe would produce. Existing entries
keep working: infoHash keys still match torrent rows, and `name:` entries expire within
30 days.

## Rationale

The denylist's burden of proof (ADR 0025) is "proven absent". A provider's 404 proves
absence for that provider only.

## Consequences

- `source_key` takes the stream's delivery route into account. `availability` and
  `headless_play` key comparisons use it.
- A torrent whose debrid link died can still play via P2P or another provider.

## References

ADR 0024, 0025, 0032. Symbols: `availability.source_key`, `availability.remember_dead`,
`availability.prune_dead`, `api.streams`, `state.dead`.
