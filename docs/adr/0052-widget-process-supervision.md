# 0052. Widget process supervision and session-bound cast controls

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** nstream maintainers

## Context

The Omarchy widget knows which cast session it displays, but headless lifecycle commands
previously rediscovered the target. A configured TV could differ from the active session.
`headless_play.auto_play` emits the local playback result only after mpv exits; detaching
it and immediately returning success hid selection and playback failures.

## Decision

`headless._lifecycle_device` resolves explicit device, active session, then discovery,
shared by stop and the other headless controls. The widget supervises local playback in
its own helper process and distinguishes a launch request from a successful result.

## Rationale

Resolve session IPs directly, as the interactive `cast_control` already does: passing them
through the name-based picker could fail or choose another receiver. Keep the existing
blocking local JSON contract rather than inventing an unverified player-start event.
A separate QML process waits for the result without holding up cast actions.

## Consequences

Local errors reach the panel, while successful requests are acknowledged without claiming
delivery. Only one supervised local playback runs per widget instance. The player has its
own process session and survives widget reloads, but the reloaded widget cannot observe
the previous helper's final result. The helper rejects oversized JSON results and discards
diagnostic stderr, which nstream already records in its own log.

## References

- `headless._headless_device`, `headless._run_stop`, `cast_control.resolve_session_device`
- `integrations/omarchy-nstream/helper.py`: `run`, `cmd_continue`, `cmd_open`
- [ADR 0031](0031-delivery-backend-reports-started.md)
