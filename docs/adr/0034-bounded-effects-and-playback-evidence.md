# 0034 — Bounded effects and explicit playback evidence

Status: Accepted

## Context

The unit suite passed while typing checks failed, installed wheels included a supposedly
excluded developer benchmark, and subprocess tests exercised only argument errors.
The single-addon path bypassed the gather budget. A late worker could close a breaker
after the collector had declared a timeout. mpv's zero exit status was mistaken for
successful media playback, and the always-loaded episode overlay also advanced movies.

## Decision

- Preserve Python, stdlib-only runtime, CLI flags, JSON fields, and existing state formats.
  `application.play_local` owns shared subtitle/language policy for TUI and headless.
  `playback.PlaybackOutcome` records media evidence; `require_started` rejects missing
  evidence and backend failures. mpv IPC and the Lua outcome file supply that evidence.
- `api._gather` owns breaker results and uses one deadline for all cardinalities.
  `net.request_budget` propagates it into retry/read work. A fixed, bounded daemon pool
  prevents a stuck system resolver from holding process exit. Running OS calls cannot
  be forcibly cancelled; pending work is cancelled and late results have no breaker effects.
- Limit JSON bodies to 8 MiB on the wire and gzip output to 32 MiB. Validate stream
  shapes and HTTP(S) URLs at addon ingestion. Only HTTP 404/410 establish removal;
  authentication, throttling, transport, and incomplete-media failures remain transient.
- Serialize persistent read/modify/write through `util.state_update`; lock contention
  beyond 100 ms skips a best-effort write. Never continue with an unlocked write.
  Preserve malformed JSON bytes in a private, uniquely named recovery file before replacing
  them. Keep current schemas, so no migration is needed for this release.
- Runtime snapshots are atomically replaced. Cast-session merges and history updates
  have separate bounded locks. History start/merge/pruning runs within one transaction.
- Playback never runs privileged firewall commands. `serve.ensure_firewall` remains
  a compatibility no-op; `firewall_hint` provides administrator guidance.
- Redact arbitrary HTTP(S) URLs, not just known provider token patterns. Machine output
  is scrubbed too. Raw mpv process output is suppressed; typed errors carry diagnostics.
- `--doctor` reads local installation state without changing permissions, starting
  backends, or contacting providers. It explicitly does not certify VPN routing or playback.

## Consequences

The existing VPN-interface heuristic remains an advisory detection mechanism, not proof
of routing or leak protection; the swarm-join gate of ADR 0032 remains mandatory.
CLI compatibility includes `--follow` JSONL: ordinary commands emit one object, follow
streams retain their event protocol. New local errors are `player_missing`, `player_failed`,
and `cancelled` (exit 130 for interruption).

Executable import contracts, fully isolated test storage, external-network guards,
subprocess protocol tests, package installation checks, and real decode/IPC smoke tests
cover different evidence layers. None establishes audiovisual correctness on a TV.

See [verification](../verification.md) for commands and the remaining hardware gate.
