# 0032. The P2P privacy gate lives at the swarm join, not at one call site

- **Status:** Accepted
- **Date:** 2026-08-08
- **Deciders:** project maintainer

## Context

Field evidence, 2026-08-08. A machine with `p2p_require_vpn: true` and **no VPN interface up**
(only `tailscale0`, a mesh, correctly not counted by `engine.vpn_active`) cast a title and the
CLI printed:

```
🌐 buffering P2P… peer 4     0.0 MB
```

The swarm was joined and the real IP was exposed to peers — the exact outcome
`p2p_require_vpn` exists to prevent. A later invocation of the _same_ title with `--quality 720`
was correctly refused with "nessuna VPN rilevata e p2p_require_vpn=true — streaming P2P
bloccato". Two runs, same config, opposite privacy behaviour.

The cause is a policy applied at one call site instead of at the operation it governs. There are
three places that resolve a stream through the P2P engine, and `_p2p_guard`
(`src/nstream/stream_select.py:639-657`) protects one:

| Site                                            | Reached by                             | Gated |
| ----------------------------------------------- | -------------------------------------- | ----- |
| `stream_select.py:424` (`_resolve_stream`)      | the primary local/cast pick            | yes   |
| `stream_select.py:357` (`_playable_url`)        | **all cast vetting and reselection**   | no    |
| `stream_select.py:568` (cached-source fallback) | a `cached` url that stopped responding | no    |

`_playable_url` is the resolver the whole cast path runs on — every `cast_vet` gate, every
language/container/video reselect. So the privacy gate was absent from precisely the flow that
resolves the most candidates, and present on the one that resolves the fewest. Which behaviour a
user got depended on which branch their invocation happened to take.

This is the failure mode ADR 0021 named for per-invocation constraints and ADR 0029 for the
advance decision: a rule enforced by _remembering to call it_ is a rule that a new path silently
opts out of. Here the cost is not a wrong resolution or a bad UX — it is a privacy guarantee the
user explicitly switched on and did not receive.

The gate is also silent on two of the three paths even when it does fire: `_playable_url`
swallows `EngineUnavailable`, and the cached-source fallback wraps it in
`contextlib.suppress`. A refusal the user cannot see is indistinguishable from a title with no
sources.

## Decision

**The privacy gate runs inside `engine.resolve` — the function that actually joins the swarm —
and nowhere else.**

1. `_p2p_guard` and `_p2p_notice_once` move from `stream_select` into `engine`, and
   `engine.resolve` calls the gate before adding the torrent. `engine` already imports `ui` and
   `Config`, and `config` does not import `engine`, so the ack persistence adds no cycle.

2. A refused resolve raises **`P2PBlocked`, a subclass of `EngineUnavailable`**. Every existing
   caller already handles `EngineUnavailable` and degrades correctly, so no call site needs to
   know the gate exists — which is the point. The subclass keeps the reason distinguishable for
   tests and for callers that want to say more.

3. **The refusal is printed once per process, by the gate itself**, not left to the caller's
   exception handler. Two of the three paths discard the exception silently; a privacy block the
   user cannot see would present as "this title has no sources". Once per process, because the
   cast vetting probes many candidates and would otherwise repeat the message a dozen times.
   The no-VPN _warning_ (when `p2p_require_vpn` is false) is deduplicated the same way — today it
   can already repeat per candidate.

4. `stream_select._resolve_stream` loses its `_p2p_guard` call. The behaviour is unchanged —
   `engine.resolve` now raises and the existing handler returns None — but the enforcement is no
   longer that site's responsibility.

**Corollary, in the ADR 0021 / 0029 mold: a caller reaches the P2P engine only through
`engine.resolve`, and does not consult the privacy gate itself. A new resolve path that reaches
the swarm some other way, or re-implements the gate, is a review defect.**

## Rationale

The alternative was to add `_p2p_guard` to the two unguarded sites. Rejected: it restores parity
today and loses it at the next resolve path, which is exactly how this defect was born — the
guard was correct when written, and `_playable_url` grew past it. Three call sites disagreeing
about a security policy is a structural problem, and a structural problem wants a structural fix.

Putting the gate in `engine.resolve` also puts it where the decision is actually meaningful:
"are we about to join a swarm" is a fact `engine` owns and `stream_select` can only approximate.
Note `_native_resolve` (debrid) is deliberately **not** gated — a debrid fetch is an HTTP GET
from a provider, it joins no swarm and exposes nothing to peers. The gate must not degrade the
one backend that is private by construction.

Raising rather than returning None was chosen so the gate cannot be ignored by a caller that
forgets to check a boolean — the same reasoning as the subclass: make the safe path the default
one.

## Consequences

- With `p2p_require_vpn: true` and no VPN, **no path resolves a P2P stream** — the cast vetting
  included. Users who relied (unknowingly) on the cast path working without a VPN will now see
  it refuse. That is the setting doing its job; the previous behaviour was the bug.
- A blocked run surfaces as `no_playable_stream` (headless) or a degraded pick (interactive),
  with the reason printed once on stderr. It is not an error code of its own: from the caller's
  point of view the stream genuinely cannot be served right now.
- `engine` gains a `config` import for the one-time ack persistence. Legal (`config` imports only
  `util`), and it keeps the notice with the operation it describes.
- Debrid-cached titles are unaffected, which is the honest fallback to offer a user without a
  VPN: it is the backend that never exposes them.
- Not fixed here, and worth stating: `vpn_active` remains a heuristic over interface names
  (`tun*`/`wg*`/…). A split-tunnel setup where the torrent traffic bypasses an up interface still
  reads as protected. Making the check real (routing/exit-IP verification) is a separate
  decision with a very different risk profile.

## References

- ADR 0021 — per-invocation constraints hold across every selection path; the corollary form used
  above.
- ADR 0029 — the structural precedent: a decision removed from the sites that kept getting it
  wrong, so they cannot get it wrong again.
- `src/nstream/stream_select.py:357`, `:421-424`, `:568`, `:639-672` (the gate and the three
  resolve sites); `src/nstream/engine.py:162-180` (`vpn_active`), `:313` (`resolve`);
  `src/nstream/config.py:172-174` (`p2p_ack`, `p2p_require_vpn`).
- Field evidence: session 2026-08-08 — `--cast "Le vite degli altri"` buffered P2P with no VPN
  under `p2p_require_vpn: true`, while `--quality 720` on the same title was refused.
