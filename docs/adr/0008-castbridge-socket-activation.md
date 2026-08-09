# 0008. castbridge daemon: systemd socket activation (retire spawn code)

- **Status:** Proposed
- **Date:** 2026-06-06
- **Deciders:** project maintainer
- **Relates to:** ADR 0007 (castbridge sender); the interim transient-unit spawn in
  `bridge.ensure_daemon` (this ADR's stepping stone).
- **Blocked on:** castbridge packaging graduating from WIP — until then the interim
  `systemd-run --user` path remains the production behaviour. See [docs/roadmap.md](../roadmap.md).

## Context

castbridge is a long-lived user daemon (AF_UNIX IPC) that must outlive the client
that happens to need it first. Today `ensure_daemon()` spawns it lazily:
`start_new_session=True` detaches the process _session_, but **not the cgroup** —
spawned as a plain child it inherits the first caller's lifecycle domain (a terminal
scope, a systemd service, a test runner) and is killed at that supervisor's teardown.
Field-found 2026-06-06: a daemon spawned inside a systemd service's cgroup survived
the TERM volley and was SIGKILLed at service stop.

Interim fix (implemented): `ensure_daemon` spawns via
`systemd-run --user --collect --unit=castbridge`, giving the daemon its own transient
unit on systemd hosts, with the detached-Popen fallback elsewhere. This closes the
lifecycle defect but keeps spawn/locking logic (flock + socket poll + dual path) in
nstream — client-side code that exists only to compensate for the daemon having no
proper home.

## Decision (proposed)

When castbridge graduates from WIP, move daemon activation to systemd socket
activation:

- `castbridge.socket` (user unit) owns the AF_UNIX socket path; `castbridge.service`
  is started by systemd on first connect (`LISTEN_FDS`/`sd_listen_fds` support in the
  castbridge C++ side, managed as a patch on the vendored openscreen clone).
- `ensure_daemon()` reduces to `_connect()`: no spawn, no flock, no poll loop, no
  fallback path. Absence of the socket unit = bridge unavailable → catt fallback,
  same contract as today.
- Unit files ship with the cast repo (which owns the binary), not with nstream;
  nstream stays a pure client.

## Consequences

- nstream loses ~50 lines of process-management code and every lifecycle concern.
- The daemon gets restart policy, journal identity, and resource accounting like any
  other user service.
- Requires patching the vendored openscreen fork (fd passing) — the cost that makes
  this Proposed-not-Accepted while castbridge still evolves.
- Non-systemd portability of the _bridge path_ is dropped (catt fallback remains);
  acceptable: castbridge is already Linux/systemd-adjacent tooling.
