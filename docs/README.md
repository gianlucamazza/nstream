# nstream documentation

Audience map — pick the page that matches the job.

| Audience | Start here | Then |
| -------- | ---------- | ---- |
| **New user** | [../README.md](../README.md) (install + quickstart) | [user/config.md](user/config.md), [user/tui.md](user/tui.md) |
| **Cast / Chromecast** | [user/cast.md](user/cast.md) | [user/troubleshooting.md](user/troubleshooting.md) |
| **Automation / agents** | [headless.md](headless.md) | [selection.md](selection.md) for ranking |
| **Contributor** | [../CONTRIBUTING.md](../CONTRIBUTING.md) | [architecture.md](architecture.md), [adr/](adr/README.md) |
| **“Why was this stream picked?”** | [selection.md](selection.md) | `nstream "<title>" --explain` |
| **Architectural why** | [adr/README.md](adr/README.md) | individual ADRs |
| **Roadmap / non-goals** | [roadmap.md](roadmap.md) | Proposed ADRs in the index |
| **Historical audit** | [archive/audit-2026-06-09.md](archive/audit-2026-06-09.md) | **not** a live backlog |
| **Verification / recovery** | [verification.md](verification.md) | Local gates, package checks, benchmarks, hardware acceptance |

## Document roles (single source of truth)

| Document | Answers |
| -------- | ------- |
| [`architecture.md`](architecture.md) | **Where** code lives (module map, import discipline) |
| [`selection.md`](selection.md) | **How** streams/audio/subs are chosen |
| [`adr/`](adr/README.md) | **Why** a trade-off was locked in |
| [`headless.md`](headless.md) | **`--json` contract** (actions, fields, errors) |
| [`user/config.md`](user/config.md) | Every config key (defaults, bounds, groups) |
| [`../CLAUDE.md`](../CLAUDE.md) | Agent/editor hard constraints + dev commands |
| [`../skills/nstream/SKILL.md`](../skills/nstream/SKILL.md) | Agent playbook; defers contract detail to `headless.md` |

Do not re-copy the module map into the README or CLAUDE.md — update `architecture.md` only.

## UI language

The interactive TUI and many CLI help strings are **Italian**. Technical docs in this tree are
**English** (project rule). When docs quote a menu entry (e.g. *Fonti stream / plugin*), the
Italian string is the real UI label.
