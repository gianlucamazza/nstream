# 0009. Movies / TV-series separation: typed home sections, typed flows

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** project maintainer

## Context

Movies and series are fully mixed in the UX while being cleanly typed underneath:

- `api.search()` already fetches the two Cinemeta catalogs **separately** and merges them
  (`src/nstream/api.py:144-149`); `api.catalog(typ, …)` exists but no CLI path calls it
  (`api.py:179-183`) — `browse()` always fuses both types (`api.py:186-192`).
- The home menu (`cli.py:1080`) offers one mixed search and three mixed browse rows; the only
  type cue in lists is the 🎬/📺 glyph from `labels.meta_label`.
- Continue-watching is one flat mixed list (`state.recent`, `cli.py:1086`/`run_continue`),
  though every history entry carries `"type"` (`state.py:37`).
- Headless `--json` infers "series wanted" only from `--season`/`--episode` (`cli.py:686-689`);
  a movie and a series with the same title (e.g. _Fargo_) cannot be disambiguated explicitly.
- The series flow (episode picker, binge auto-advance, per-episode resume) lives inline in
  `cli.py` behind 7 scattered `if typ == "series"` branches (`_play_series` 363-407,
  `play_meta` 410-446, `play_history` 455-488), part of why cli.py is the audit's biggest
  hotspot (1319 LOC, finding #2 of `docs/audit-2026-06-09.md`).

## Decision

nstream separates movies and TV series at four levels, keeping the global mixed surfaces as
the default for back-compat:

1. **Typed home sections** — the home menu gains `🎬 Film` and `📺 Serie TV` rows, each opening
   a type-scoped section: its own type-filtered continue-watching, `Cerca…`, and the three
   catalogs (Popolari / Novità / Top IMDb) served by the already-existing `api.catalog(typ, …)`.
   Global `Cerca…` and the top-level continue-watching stay mixed.
2. **Typed search & browse plumbing** — `api.search(cfg, query, typ=None)`,
   `run_search(…, typ=None)`, `run_browse(…, typ=None)`: `None` keeps today's mixed behaviour.
3. **Typed continue-watching** — `state.recent(cfg, typ=None)` filters on the entry's existing
   `"type"` field; used by the typed sections (and by `-c` via flags below).
4. **Explicit type flags** — mutually exclusive `--movies`/`--series` on the CLI, honoured by
   interactive search/browse/continue **and** by headless `--json` `_select_meta` (which today
   only infers series from `--season`/`--episode`; the flags become the explicit tie-breaker).
5. **Series flow extraction** — the series-only logic (`_play_series` binge loop, the episode
   picker half of `play_meta`, the series-resume branch of `play_history`, `ep_preview`) moves
   to a new `src/nstream/series.py`. It receives the player entry point as an injected callable
   (the `on_save` pattern already used at `cli.py:385`), so it **never imports `cli`** and the
   import-graph discipline holds. `play_meta` becomes a thin type dispatch.

## Rationale

Options considered for the TUI interaction:

| Option                                               | Trade-off                                                                                                                                                             |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| fzf type-toggle key (Ctrl-T cycles all/movie/series) | Zero menu depth, but invisible (needs the hint header) and leaves browse catalogs mixed                                                                               |
| **Typed home sections** (chosen)                     | One extra menu level, but discoverable, gives per-type catalogs + per-type continue-watching for free, and matches "home = rich entry surface" (`run_home` docstring) |
| Separate binaries/subcommands (`nstream movies`)     | Over-engineering for a personal CLI; breaks muscle memory                                                                                                             |

The plumbing is deliberately parameter-based (`typ=None` = mixed) rather than a fork of the
flows: every existing entry point keeps its behaviour, the skill's headless contract is only
extended (new optional flags), and the diff stays reviewable. The `series.py` extraction rides
the same convention as the `stream_select`/`subs`/`labels` extractions already done.

## Consequences

- Two new fzf prompts (`film> `, `serie> `) and a submenu layer; home stays the single entry.
- `series.py` + `tests/test_series.py` (new convention-matching pair); cli.py shrinks by
  ~120 lines and loses most `if typ == "series"` branches; the remaining audit refactors
  (cast-flow helper, `headless.py`) become easier on a smaller cli.
- `--movies`/`--series` become part of the headless contract the `nstream` skill can use to
  disambiguate same-title media without guessing via `--season`.
- Genre browsing per type (`api._extras` already supports `genre=`) becomes a natural
  follow-up inside the typed sections; out of scope here.
- Cost accepted: the home menu code gains a section dispatch; mixed search results keep
  needing the 🎬/📺 glyphs (the global surfaces don't go away).

## References

- `docs/audit-2026-06-09.md` (cli.py size finding; backlog)
- `src/nstream/api.py:144-192`, `src/nstream/cli.py:363-488`, `src/nstream/cli.py:1080-1128`,
  `src/nstream/state.py:37-42`
- Implementation plan: see "Piano di implementazione" in the session that proposed this ADR
  (waves: plumbing → home sections → flags/headless → series.py extraction → tests per wave).
