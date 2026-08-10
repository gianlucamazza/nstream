# 0033. Exhaustion raises; `None` means the user backed out

- **Status:** Accepted
- **Date:** 2026-08-10
- **Deciders:** nstream maintainers

## Context

`stream_select.prepare_stream` returned `None` for two unrelated outcomes: the user pressing
ESC in a picker, and _nothing being playable_. `cli._play_video` cannot tell them apart, so it
returned `notice=None` for both — and the notice is what becomes the fzf header when the menu
comes back. The header is the only user-facing channel that survives the menu's fullscreen
redraw; the `ui.status(..., kind="fail")` lines emitted deeper in `_pick_stream` are written to
stderr and erased by the next fzf draw.

The failure this fixes, observed on a real title: every source for it was a pure torrent (an
expired debrid returned no direct link), and the ADR 0032 privacy gate refused each swarm join
(`p2p_require_vpn` on, no VPN up). `engine.resolve` raised `P2PBlocked`, `_resolve_stream`
logged it at INFO and returned `None`, `pick_and_resolve` returned `None`, `prepare_stream`
returned `None` — and the TUI dropped back to the catalog with no message at all. Every piece
of the diagnosis existed somewhere; none of it reached the user.

## Decision

On the selection path `None` means **the user backed out** and nothing else. Exhaustion raises
`stream_select.NoPlayableStream`, carrying a `reason` string that callers surface:
`cli._play_video` returns it as the notice (→ fzf header), `headless_play` folds it into the
`no_playable_stream` JSON message.

## Rationale

The alternative — returning a `(result, reason)` tuple — spreads the unpacking across every
call site and lets a caller keep ignoring the reason, which is exactly the bug. An exception
matches the three siblings this module already raises for the same class of outcome
(`QualityUnavailable`, `AudioLangUnavailable`, `ContentTooShort`) and makes the silent path
impossible to reintroduce by accident: a new exhaustion branch with no message has nowhere to
return to.

The reason itself is computed by `stream_select.unresolvable_reason` from the _shape_ of the
result set (no direct link, only torrents) plus `engine.p2p_block_reason` — the gate's
predicate without its side effects. No provider is named or probed, so the debrid-agnostic
constraint holds: the message describes what nstream sees, never who the user's provider is.

## Consequences

- Callers of `prepare_stream` must handle `NoPlayableStream`; the two that exist (`cli`,
  `headless_play`) do. `QualityUnavailable` still wins when a hard tier is what emptied the
  set, since it can also list the tiers that do exist.
- `_pick_stream` raises on an empty ranking instead of returning `None` — an empty fzf menu
  never opened anyway, so that `None` was pure ambiguity.
- `--explain` gains a `SORGENTI: n/m con link diretto · k torrent` line (and
  `counts.direct_links` / `unresolvable_reason` in JSON), so the same diagnosis is available
  before attempting playback.

## References

- ADR 0032 (P2P privacy gate at the swarm join) — the gate whose refusal was invisible.
- ADR 0025 (dead-source classification) — the other exhaustion branch, now equally loud.
- Symbols: `stream_select.NoPlayableStream`, `stream_select.unresolvable_reason`,
  `stream_select.prepare_stream`, `stream_select.pick_and_resolve`, `stream_select._pick_stream`,
  `engine.p2p_block_reason`, `cli._play_video`, `headless_play._emit_unplayable`,
  `explain._links_section`.
