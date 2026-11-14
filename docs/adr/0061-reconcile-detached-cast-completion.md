# 0061. Reconcile detached cast completion

- **Status:** Accepted
- **Date:** 2026-11-15
- **Deciders:** maintainer
- **Amends:** 0054, 0056, 0057

## Context

After control loss, `state.active_cast` deliberately detaches its observer and retains
playback presence. The native daemon can subsequently reconnect and observe completion
without any follower remaining to clear that record. The desktop then displays a film
that has finished until the detached-session TTL expires.

## Decision

Started events bind presence to the daemon's Unix peer PID/start time and receiver media
session ID. Each desktop heartbeat reads the already running daemon's cached snapshot,
with a short socket timeout. Matching explicit finished, stopped or interrupted events
retire the presence and its matching legacy watch fallback.

Disconnected/unclassified idle snapshots and another daemon/media generation preserve
presence. `remux._live_load` rebinds intentional HLS reloads to the same logical session.
Reading the cache never starts a daemon, queries a TV or sends a playback command,
including when the plugin panel is closed.

## Consequences

Completion can clear detached presence without owning playback or polling the receiver.
Older sessions/backends lacking this binding keep their conservative TTL behavior.
A daemon restart cannot prove completion of a record belonging to its predecessor.
This does not change the rule that loss of control is not playback completion.

## References

`bridge._peer_key`, `bridge.peek_snapshot`, `state.active_cast.reconcile_terminal`,
`state.active_cast.rebind_cast`, `desktop.watch`; ADRs 0054, 0056, 0057.
