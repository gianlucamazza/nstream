# 0060. Transactional playback transfer

- **Status:** Accepted
- **Date:** 2026-11-15
- **Deciders:** maintainer

## Context

Continue-watching resumes saved history and can choose another release. It cannot safely
move an active film: its cursor may be stale, history may be disabled and the source
must survive destination preparation. mpv Alt+C previously quit before cast readiness.

## Decision

The core use case `handoff.transfer` resolves the exact video ID and filters candidates
by the original torrent/file identity or release filename. It runs normal selection,
language, subtitle, compatibility and P2P privacy policies through `headless_play.auto_play`.
The optional Omarchy plugin and TUI invoke this same transaction.

`state.playback_session` records confirmed mpv presence independently of history.
Descriptors contain IDs, release identifiers and playback preferences; they never contain
source URLs. A private pipe starts an independent destination worker. Local workers own
mpv and temporary files until playback ends; cast workers use detached delivery.

The source continues during preparation. Once the destination reports playback, the
coordinator checks durations, pauses both players, reads the live source cursor, seeks the
destination, verifies cursor and pause state, and only then stops the original source.
A failed preparation leaves the source untouched. Commit failures restore it and retire
only the owned destination. If source retirement is uncertain, retain the verified
destination and report `transfer_incomplete`.

## Rationale

Reusing ordinary source resolution avoids leaking expiring credentials between processes
and preserves existing compatibility/privacy gates. It costs another resolve and may
require remux preparation. Private operation IDs, logical session IDs, receiver content
fingerprints and process start identities reject stale operations. A runtime lock
serializes transfer coordinators; an already occupied destination is rejected.
There is no atomic protocol spanning independent receivers: observation and commands
remain separate operations, so failures are explicit and rollback is conservative.

## Consequences

- Legacy sessions without exact media/release metadata and mirror sessions cannot transfer.
  A fresh playback start publishes the required metadata.
- Missing releases, unknown/different durations and unverifiable receiver identity fail
  safely. Duration tolerance is five seconds or 0.1%, whichever is larger.
- The selected language and subtitle preferences travel; external caption files are
  acquired again. Automatic subtitle alignment may differ after reacquisition.
- Transfers do not automatically advance episodes. The exact season/episode is retained.
- TorrServer started by the source survives while a confirmed destination leases its port.
- Delivery retirement never sends a second STOP or reaps another cast's serving resources.
- Core playback and transfers work without Omarchy or its plugin.

## References

`handoff.Endpoint`, `handoff.commit`, `local_control`, `media_identity`,
`state.playback_session`, `application.play_local`, `engine._shutdown`,
`remux.retire_delivery`; ADRs 0021, 0031, 0032, 0056, 0057.
