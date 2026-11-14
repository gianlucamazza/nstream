# 0057. Versioned desktop observation without playback ownership

- **Status:** Accepted
- **Date:** 2026-11-14

## Context

The desktop plugin previously launched separate CLI processes for history and receiver
status. Their independent results could refer to different playback generations.
Device addresses were confused with discovery names, and a failed receiver query looked
like IDLE. A plugin reload must never stop a cast or acquire ownership of its HLS server.

## Decision

`desktop.watch` exposes versioned JSONL snapshots through `nstream --json --watch`.
A single persistent process emits local presence, bounded history, capabilities and
receiver telemetry. Stdin carries only receiver-interest updates (`observe`); playback
commands continue to use the public CLI with an optional `--session-id` guard.

`desktop.Observer` permits one bounded receiver request at a time on a daemon worker.
It discards results from replaced sessions, retains the last valid sample on failure,
and reports connection state and sample time independently of playback state. Receiver
polling requires explicit interest; closed panels cause no new receiver requests.

## Rationale

Persistent stdio is supported by Quickshell and Python stdlib. It avoids an extra daemon,
socket permissions, activation and transport dependencies for a single desktop consumer.
The CLI remains usable for scripts and the plugin never speaks castbridge IPC directly.
MPRIS may later expose standard media controls, but cannot replace history and subtitle
controls. The observer still polls the receiver at a bounded rate: JSONL is push to the
plugin, not a claim that the receiver itself supplies push updates.

## Consequences

The plugin validates protocol major version and required capabilities before enabling
controls, reconnects with bounded backoff and marks stale readings visibly. Session IDs
are separate from device identifiers; legacy watch records have a stable generation hash.
Commands reject stale sessions at dispatch. The guard is best-effort across independent
processes, not an atomic receiver transaction. Responses from superseded sessions are
ignored by the plugin. EOF or broken stdout ends observation only. All output uses the
shared redacting writer; no stream URLs or tokens enter the protocol.
