# 0037. Interactivity is an explicit mode; the domain never opens fzf

- **Status:** Accepted
- **Date:** 2026-10-01
- **Deciders:** maintainer

## Context

An architecture audit (2026-10-01) found that whether nstream may prompt is decided by TTY
detection deep in the domain, not by the caller's mode:

- **Cast language prompt.** `cast_flow.run_cast` checks `sys.stdin.isatty()` before the
  ADR 0035 "Parto subito in …?" prompt.
- **Size-cap confirm.** `remux._confirm` uses `picker.confirm(non_tty_default=True)`.
- **Device picker.** The `caster` device picker is gated the same way.

So a `--json` run started from a terminal (or a pty, as agents use) can block on fzf. That
contradicts the headless contract ("never opens fzf", `headless` module docstring) and
ADR 0035 ("Headless takes the direct cast"). The domain also imports the TUI:

- `stream_select`, `caster`, `remux` and `cast_flow` import `picker`/`ui`;
- `api` and `engine` import `ui`;
- `engine` writes config.

`tests/test_architecture.py` checks only "stdlib-only, nothing imports cli, no cycles",
while `docs/architecture.md` says the invariants are enforced.

The same audit found duplicated policy between the TUI and `--json` paths:

- exception→message mapping is written four times;
- there are five JSON emitters, and only one scrubs URLs and tokens;
- `--explain`/`--probe` rank without `prune_dead`/native-cached marking;
- ADR 0031 is enforced only headless, and ADR 0033 is incomplete for manual picks.

## Decision

1. **`PlayOpts.interactive: bool` is set by the frontend.** The TUI sets True and
   `--json` sets False. It is threaded through `run_cast`, `remux` and `caster`. Domain code
   never calls `isatty()` to decide whether to prompt.
2. **Prompts are injected.** Domain modules receive a `Prompter`, a small protocol with
   `confirm(msg, default)` and `choose(rows, prompt)` that `cli` builds over fzf. Headless
   passes a non-interactive prompter that returns the documented default. After the
   migration, `stream_select`, `cast_flow`, `cast_vet`, `caster`, `remux`, `api` and
   `engine` no longer import `picker`.
3. **`tests/test_architecture.py` gains a forbidden-edges table** that encodes the domain
   tiers of `docs/architecture.md` (selection/cast/discovery must not import
   `picker`/`labels`).
4. **One error and output model.**
   - Each domain exception carries `code` and `message`; frontends only render them.
   - All machine output goes through one `jsonio.emit` that applies `log.public_value`.
5. **One candidate pipeline.** `stream_select.prepare_candidates(cfg, results, opts)`
   (prune dead, native-cached mark, quality resolution) is shared by prepare, explain and
   probe.

## Rationale

TTY detection answers "is there a terminal?", not "may this run ask a question?". Agents
run `--json` under a pty, so the two diverge in exactly the case the contract protects.
Injecting the prompter is the same pattern `series.PlayVideo` already uses for playback. It
keeps the domain testable without monkeypatching `sys.stdin`.

Alternatives:

| Option                                              | Verdict                                             |
| --------------------------------------------------- | --------------------------------------------------- |
| Keep TTY checks, add a `NSTREAM_HEADLESS` env guard | Rejected: still implicit, and still imports the TUI |
| Explicit flag only, keep `picker` imports           | Partial: fixes the bug, not the layering            |
| Explicit mode + injected prompter + enforced edges  | **Proposed**                                        |

## Consequences

- A mechanical but wide refactor across the cast and selection paths. It lands in steps:
  first the flag (bug fix), then the prompter, then the edge table.
- `docs/architecture.md` is corrected to the real one-way graph.
- Test seams change from `monkeypatch(sys.stdin.isatty)` to passing a fake prompter.

## References

ADR 0021, 0031, 0033, 0034, 0035. Symbols: `cast_flow.run_cast`, `remux._confirm`,
`picker.confirm`, `stream_select.prepare_stream`, `explain._rank`, `headless._emit_json`,
`headless_play.emit_json`, `tests/test_architecture.py`.
