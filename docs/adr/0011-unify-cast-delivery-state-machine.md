# 0011. Unify the castbridge→catt cast-delivery state machine

- **Status:** Proposed
- **Date:** 2026-07-13
- **Deciders:** project maintainer

## Context

The bridge event loop — consume `bridge.cast_load` events, decide "failed before `started` →
fall back to catt, failed after → no fallback", track `pos`/`dur` from `playing`/`ended`,
handle KeyboardInterrupt — is implemented **three times**:

- `caster.py` `_cast_via_bridge` (direct Tier-1 cast of a remote URL);
- `remux.py` `_cast_file_via_bridge`, no-follow branch (Tier-2: Range server + detached state);
- `remux.py` `_cast_file_via_bridge`, follow branch (Tier-2: in-process server, teardown at end).

The catt fallback is itself duplicated with different shapes: `caster._cast_via_catt`
(foreground `subprocess.run` + status poll loop) vs the `remux.cast_file` catt branch
(detached `Popen` serving the file + `_await_start` + `RunState`). The semantics legitimately
differ — a remote URL needs no server lifecycle, a served file does — but the _policy_ layer
(fallback decision, started/pos/dur tracking, `_CAST_DONE` finish heuristic, event forwarding
to the `--follow` JSONL callback) is copy-shaped. Evidence of drift already exists: the
`finished = pos >= dur * _CAST_DONE` advance heuristic lives only in `caster.py`; the
`disconnected` EOF handling only in `remux.py`'s loops. These are the least-exercised paths
in the codebase (bridge startup failures), so divergence surfaces late. The 2026-07-13
architecture review rated this the codebase's only MEDIUM structural finding; the M3 fix
(`subs_delivered`) had to touch all three copies, confirming the cost.

## Decision

Extract one shared delivery driver — a small `cast_delivery.py` (or a `caster`-internal
helper importable by `remux`) owning: (1) the bridge event-consumption loop with the
started/failed/ended policy and pos/dur/finished accounting, parameterized by an
`on_started`/`on_ended` hook pair; (2) the fallback _decision_ (never the fallback
_mechanics_, which stay per-delivery: remote-URL catt vs serving catt). `caster` and both
`remux` branches become thin adapters that supply their lifecycle hooks (server spawn/teardown,
`RunState` writes) and their catt fallback callable.

## Rationale

Options considered:

1. **Status quo** — three copies, kept in sync by review. Rejected: drift is already
   observable and every cross-cutting change (subs_delivered, future event kinds) costs 3×.
2. **Full unification including the catt paths** — one state machine for everything.
   Rejected: the two catt shapes (blocking poll vs detached serve) differ for real,
   documented reasons (ADR 0005/0007); forcing them into one body would trade duplication
   for conditionals — worse to reason about.
3. **Unify the bridge loop + fallback policy only** (chosen) — the part that is genuinely
   copy-shaped, leaving delivery mechanics where they belong.

## Consequences

- One place to fix event-policy bugs; `--follow` JSONL behaviour becomes uniform by
  construction (today `disconnected` and the finish heuristic differ between paths).
- A new small module (or shared private helper) below `cli`; `remux` gains an import on it
  (already imports `caster`, so no layering change).
- Migration must be behaviour-preserving and test-pinned: the existing `test_caster.py` /
  `test_remux.py` bridge/fallback tests are the spec; they must pass unchanged (modulo
  monkeypatch seams).
- Until accepted and implemented, cross-cutting changes must be applied to all three copies
  — this ADR is the reminder of where they live.
- Known single-slot limits to revisit here: `RunState("remux")` (a second Tier-2 cast to a
  different TV reaps the first one's server) and `RunState("watch")` (one cast session) are
  both single-slot by design — fine for one TV, wrong for multi-device. If multi-device ever
  matters, per-device slots belong in the unified delivery layer.

## References

- `src/nstream/caster.py` (`_cast_via_bridge`, `_cast_via_catt`)
- `src/nstream/remux.py` (`_cast_file_via_bridge`, `cast_file` catt branch)
- ADR 0005 (Tier-2 remux delivery), ADR 0007 (castbridge sender), ADR 0008 (socket activation)
- Architecture review 2026-07-13 (MEDIUM finding: triplicated state machine)
