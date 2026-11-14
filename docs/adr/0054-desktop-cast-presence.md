# 0054. Desktop cast presence independent of watch history

- **Status:** Accepted
- **Date:** 2026-11-14

## Context

The Omarchy plugin requires `--json --history`, absent from nstream 1.44.0.
`state.remember_cast` records only detached casts with history enabled, so it cannot
describe playback while `cli._play_on_cast` blocks following the receiver.

## Decision

`state.active_cast.track_cast` observes backend events at `cast_flow.run_cast` and
publishes confirmed presence separately from resume history. `headless._run_history`
exposes descriptive history fields and presence in one local, network-free read.

## Rationale

Reusing the watch record would couple remote controls to history preferences and
require full history metadata inside delivery code. A separate runtime descriptor
needs only title, device and lifecycle ownership. An owner PID plus process start
identity bounds followed presence; the existing six-hour TTL bounds detached presence.

## Consequences

The slot is written only after a backend reports `started`. Failed starts preserve
earlier playback. Followed calls clean their own slot on return; disconnected observers
detach it because observer loss does not prove playback ended. Session identifiers
prevent older followers from erasing replacement sessions. Successful stop clears the
matching device. Presence does not write watch history or contain stream URLs.

Legacy watch sessions remain a fallback for already detached casts. A process started
before this change cannot publish the new descriptor retroactively.
