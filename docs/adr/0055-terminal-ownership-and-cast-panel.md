# 0055. Terminal ownership independent of cast monitoring

- **Status:** Accepted
- **Date:** 2026-11-14

## Context

Concurrent source, audio and subtitle updates used carriage returns on the same stderr
line. Notices could split those updates, and opening fzf hid keyboard help. The blocking
cast follower also owned keyboard input and working files, making a return to home
indistinguishable from an interrupt that stopped the receiver.

## Decision

`tui_output.session` installs the interactive output owner only on an explicit TUI
path with stdin/stderr TTYs. Structured activity updates belong to a `Panel` selected
by context; copied contexts preserve ownership across worker and executor boundaries.
The renderer serializes writes and suspends rendering while fzf or mpv owns the terminal.
Domain notices keep their capture contract and offer an optional presentation sink;
legacy prints and debug stderr share the same terminal owner. Details are bounded,
sanitized and redacted. Non-interactive output and JSON do not install this renderer.

`tui_cast.CastTask` follows the existing cast flow in a worker and owns its working
directory until delivery ends. Only the frontend reads keys or executes injected menus;
worker prompts are marshalled to the main thread. Esc/Ctrl-C after confirmed startup
returns to home without interrupting delivery. Stop is an explicit lifecycle action.
Startup cancellation cooperates with existing backend cleanup paths via
`util.cancel_scope`; aborting a new preparation does not stop an earlier receiver session.

The frontend reuses `state.active_cast_info` for confirmed presence, with the legacy
watch session as fallback. Reopening a managed cast reuses its monitor. Opening a cast
launched elsewhere observes and controls it without issuing another LOAD. Audio changes
run the shared selection/vetting flow again at the observed position, preserving quality
and the P2P privacy gate; the delivery worker never competes for stdin.

## Rationale

A lock around individual prints cannot preserve multiple activities on one progress
line or coordinate terminal ownership with fzf. A separate fullscreen UI framework
would duplicate the existing picker and add runtime dependencies. Separating the
frontend from delivery keeps the compact panel compatible with existing backends.

## Consequences

Menus inherit the terminal palette, use portable single-cell symbols by default, and
keep navigation hints separate from notices. The home shows actions before collections;
continue-watching has its own list. Panels adapt to terminal width without wrapping
redrawn rows. Unknown progress or receiver values are not invented.

Working files and serving processes survive returning to home in the same process.
This decision does not promise persistence after the application exits. Attached series
completion still returns the existing advance result; returning home leaves the current
item playing and does not launch subsequent episodes behind an open menu. Mirror exposes
only supported panel actions (home, details and stop); receiver commands remain on the
DMR path. Automated tests use fake receivers and PTYs; audiovisual acceptance remains a
separate receiver check.
