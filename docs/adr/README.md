# Architecture Decision Records

Short, immutable notes recording an architectural decision: the context that forced it,
the choice made, and the consequences accepted. They explain *why* the code is shaped the
way it is — the companion to `docs/selection.md`, which explains *how* one subsystem behaves.

Format: MADR-light (Context · Decision · Rationale · Consequences · References), kept terse
and code-anchored (cite `src/nstream/<mod>.py:line`) in the project's documentation tone.

An ADR is append-only: once **Accepted** it is not edited to reflect a later change of mind.
A new ADR supersedes it instead, and the old one is marked `Superseded by NNNN`.

| # | Title | Status |
|---|-------|--------|
| [0001](0001-native-debrid-resolver-adapter-layer.md) | Native debrid resolver adapter layer (alongside Torrentio) | Accepted |
| [0002](0002-realdebrid-native-integration.md) | RealDebrid: stay on Torrentio, no native cache path | Accepted |
| [0003](0003-torbox-native-integration.md) | TorBox native resolver | Accepted |
| [0004](0004-premiumize-native-integration.md) | Premiumize native resolver | Accepted |

New ADR: copy [`0000-template.md`](0000-template.md), take the next number, add a row above.
