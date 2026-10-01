# Roadmap, non-goals, and open decisions

## Non-goals

nstream intentionally will **not**:

- Bundle Electron, a web UI, or a Node server
- Take runtime Python third-party dependencies (stdlib + external CLIs only)
- Ship a multi-user / cloud account system
- Hard-code third-party debrid or addon API tokens
- Log or emit stream URLs or debrid tokens in clear
- Replace a full Stremio marketplace (user-pasted manifests only)

## Open architectural proposals (Proposed ADRs)

Track status in [adr/README.md](adr/README.md). Review periodically: implement, defer with
date, or reject via a superseding ADR.

| ADR | Title | Notes |
| --- | ----- | ----- |
| [0008](adr/0008-castbridge-socket-activation.md) | castbridge systemd socket activation | Interim: `systemd-run` transient unit in `bridge.ensure_daemon`. Full socket activation still **Proposed** — blocked on castbridge graduating from WIP packaging. |
| [0013](adr/0013-custom-cast-receiver.md) | Custom CAF receiver | Would reduce Tier-2 remux and unlock track switching. Large external build; remains **Proposed**. |
| [0037](adr/0037-explicit-interactivity-and-injected-prompts.md) | Explicit interactivity + injected prompter | `--json` under a pty can block on fzf; domain imports the TUI. Awaiting acceptance. |
| [0038](adr/0038-dead-source-key-by-what-failed.md) | Dead-source key by what failed | A debrid 404 bans the whole torrent for 30 days. Awaiting acceptance. |

## Possible product extensions

- Trakt (or similar) watch-history sync
- Richer catalog addons beyond Cinemeta-shaped sources
- Packaging of castbridge / mirror binaries alongside nstream (still separate projects today)

These are ideas, not commitments. Prefer a new ADR when an extension forces an architectural
trade-off.

## Release process (maintainer)

1. Bump `__version__` in `src/nstream/__init__.py` (hatch dynamic version).
2. Keep `packaging/PKGBUILD` `pkgver` in sync.
3. Update [CHANGELOG.md](../CHANGELOG.md).
4. Tag; build Arch package via `packaging/build-local.sh` when needed.
5. Do **not** commit `packaging/pkg/`, wheels, or `*.pkg.tar.zst` (see CLAUDE packaging hygiene).
