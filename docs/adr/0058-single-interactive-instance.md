# 0058. One interactive instance per graphical display

- **Status:** Accepted
- **Date:** 2026-11-14

## Context

The desktop entry and plugin could each spawn a new foot window. A widget-local busy
flag only covered the helper's short launch request, so repeated clicks created multiple
TUIs capable of controlling the same cast. Manual terminal invocations also had no guard.

## Decision

`launcher.main` is the shared `nstream-launch` entry point. It holds a nonblocking advisory
lock for the dedicated terminal's lifetime, uses the stable `nstream` app ID, and focuses
an existing window on Hyprland. `nstream-fuzzel` remains a compatibility wrapper.

`cli._dispatch` holds a separate `launcher.interactive_instance` lifetime lock for all
interactive playback/browsing sessions, including direct terminal invocations. A second
invocation focuses the owning terminal and exits without starting another TUI. The owner
PID is checked against its process-start identity. JSON observation/control processes and
noninteractive explain calls do not acquire the TUI lock.

## Rationale

Kernel locks serialize simultaneous launches and disappear on process exit without stale
PID cleanup. Keeping the lock inode avoids unlink/recreate races. Runtime files use
O_NOFOLLOW, private permissions and ownership checks. Display-scoped locks allow separate
graphical sessions. The interactive lock remains best-effort on disk errors, consistent
with playback policy; the desktop launcher fails closed if its lock cannot be secured.

## Consequences

Hyprland activation uses exact window addresses and owner ancestry; old dedicated foot
windows titled `nstream` remain discoverable. On other compositors the singleton still
works but bringing a window forward may require manual focus. Existing processes started
before the guard cannot acquire it retroactively and are not terminated. Repeated direct
interactive commands bring forward the existing TUI; their new query is not executed.
No lock is inherited by playback helpers and no receiver STOP/LOAD is sent on activation.

Omarchy and its plugin remain optional external consumers of nstream's public interfaces.
Neither is required for the CLI, instance guard, JSON controls or observer. Hyprland focus
and foot are optional desktop-launcher capabilities, not core playback dependencies.

Activation acknowledgements and explicit rejection of new interactive requests are
refined by [ADR 0059](0059-desktop-activation-acknowledgements.md).
