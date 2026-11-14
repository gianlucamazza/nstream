# 0059. Acknowledge desktop activation and reject ignored interactive requests

- **Status:** Accepted
- **Date:** 2026-11-14

## Context

Spawning a launcher process is not evidence that a window opened or gained focus.
The optional desktop client needs a bounded result while a supervisor keeps the terminal
singleton lock alive. Repeated interactive commands must not silently ignore a new query.

## Decision

`nstream-launch --json` starts a detached stdlib supervisor and receives one acknowledgement
through a private inherited pipe. The supervisor reports `opened` after observing its
window or its own TUI owner, `focused` after successful activation, and `already_open`
when a verified TUI exists without compositor activation support. Known focus failure is
an error and never causes a second window. The supervisor retains the lifetime lock after
reporting, including after an unconfirmed startup timeout; clients never own that lifetime.

The JSON object has `api_version: 1`, `action: launch`, `ok`, `result` and `error`.
Startup observation is bounded to eight seconds and the client pipe wait to twelve seconds.
Errors are stable labels (`launch_failed`, `launch_timeout`, `launch_busy`, `focus_failed`,
`terminal_missing`). No URL, title or receiver data enters this protocol.

A second bare interactive invocation remains activation. A second query, continue or
browse request exits 2 with an explicit not-executed message. JSON automation remains
independent. This refines the activation/reporting behavior in ADR 0058.

## Rationale

A pipe needs no network port, extra daemon, socket discovery or runtime dependency.
An acknowledgement separates startup errors from normal process supervision. Sending
new queries into a live fzf session would introduce a new stateful command channel;
explicit rejection preserves user control and makes exit status truthful.

## Consequences

The plugin waits for the acknowledgement, keeps failures visible, and does not close its
panel merely because a helper spawned. `already_open` is shown as a manual-focus hint.
The QML action tests execute the real button handler and panel functions in Qt Quick Test
with desktop services substituted; Omarchy is not required in either the core or CI.
