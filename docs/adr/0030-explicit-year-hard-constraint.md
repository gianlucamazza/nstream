# 0030. An explicit `--year` is a hard constraint on headless title selection

- **Status:** Accepted
- **Date:** 2026-08-08
- **Deciders:** project maintainer

## Context

Field evidence, 2026-08-08: `nstream --json --cast --movies --year 2006 "Le vite degli altri"`
cast **Cash Truck** (`tt0347330`, 2004). The requested year was discarded and an unrelated film
was sent to the TV.

`_select_meta` (`src/nstream/headless.py:65-89`) applies `year` only as a tie-breaker **inside**
the set of exact normalized-name matches:

```python
exact = [m for m in metas if _norm_title(m.get("name", "")) == q]
if year:
    by_year = [m for m in exact if str(m.get("releaseInfo", "")).startswith(year)]
    exact = by_year or exact
if exact:
    return exact[0], "exact"
return metas[0], "first"
```

Two independent failures follow:

1. **The year never reaches the fallback.** Cinemeta's catalog rows carry an English/original
   `name`, so an Italian query matches nothing, `exact == []`, and line 89 returns `metas[0]` —
   `year` had zero influence. The `Meta` TypedDict (`src/nstream/types.py:13-29`) has no alias
   or localized-title field, and neither the catalog rows nor `api.meta` supply one, so matching
   a localized title on `name` is impossible with the data available. The year was the one usable
   disambiguator present, and it was thrown away.
2. **Even inside `exact` the year is soft.** `by_year or exact` silently falls back to the whole
   exact set, so `--year 2006` on a wrong-year exact match still returns `selection == "exact"`.

Compounding: `_norm_title` (`src/nstream/headless.py:61-63`) folds case and punctuation but not
accents, while `api._norm_text` (`src/nstream/api.py:69-81`) already does NFKD folding — so
"Amélie" vs "amelie" also fails the exact test. And `args.year` (`src/nstream/cli_args.py:102`)
has exactly one consumer (`headless.py:166`); it never reaches `api._search_score`
(`src/nstream/api.py:84-92`), whose year component only fires on a year typed _inside_ the query
string.

`selection` ("exact" / "first") is emitted in the result JSON
(`src/nstream/headless_play.py:377`) but **no branch in the source tree reads it**. The caller is
told it was a guess and does nothing with that.

The failure mode matters more headless than interactive. In the fzf path a human confirms the
title on screen; headless, an agent parses `ok: true` and the wrong film plays.

## Decision

In the headless path, the year becomes a filter over the **whole** candidate set, and an
explicitly requested year is a **hard constraint** — but it refuses only on proven contradiction.

1. **Year-first, over all `metas`.** A tri-state `_year_matches(meta, year) -> bool | None`:
   `True` when `releaseInfo` parses and contains `year` (handling `"2006"`, the closed range
   `"2006-2010"`, the open range `"2006-"`, and the en-dash variant Cinemeta emits for series);
   `False` when it parses and excludes it; **`None` when `releaseInfo` is absent or
   unparseable**. The pool is `hits or unknown or metas`, and the exact-name test then runs
   _inside_ the pool.

2. **A third selection tier, `"year"`** — the year disambiguated, the name did not. This is
   exactly the localized-title case: the right meta was in `metas` all along, ranked below a
   wrong-year film, and the caller is told which evidence picked it.

3. **Refusal only on proven contradiction.** When the year was explicit **and** every candidate
   has a _known_ year **and** none matches, nstream emits
   `{"ok": false, "error": "no_result", "years": [...]}` and exits 1 instead of playing a guess.
   An unknown `releaseInfo` never refuses — it falls through on the `unknown` pool.

4. **An inferred year stays soft.** The trailing-4-digit-token split
   (`src/nstream/headless.py:78-82`) turns `"dune 2021"` into query + year. That inference may
   only reorder, never refuse: `"blade runner 2049"` and `"1917"` are titles, and a hard
   constraint would make them unplayable.

5. **One normalizer.** `api._norm_text` is promoted to public `api.norm_text`, its one internal
   caller updated, and `headless._norm_title` is deleted. NFKD accent folding everywhere.

## Rationale

The constraint belongs in the headless layer, not in `api.search`. `api._search_score` feeds the
interactive fzf list too, where a hard year filter would hide titles from a user who can see and
correct the mistake. Headless is the only layer where "no human will notice the wrong film" is
true, so it is the only layer that should refuse.

| Option                                                  | Verdict                                                                                                                         |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Feed `args.year` into `api._search_score`               | Rejected — changes interactive ranking for a `--json`-only flag; still only reorders, so the wrong film can still win.          |
| Refuse whenever the tier is `"first"`                   | Rejected — Cinemeta omits `releaseInfo` often enough that this breaks ordinary playback. Refuse on contradiction, not on doubt. |
| Resolve localized titles via an alias source            | Rejected here — no alias field exists in the catalog or in `api.meta`; it needs a new data source and is a separate decision.   |
| Hard year filter over all `metas`, tri-state on unknown | **Chosen** — uses evidence already in hand, and the tri-state keeps the false-negative risk where it belongs.                   |

The tri-state is the whole design. A two-state `_year_matches` would treat "no `releaseInfo`" as
"wrong year" and refuse to play a large share of the catalog — trading a rare wrong film for a
common false negative.

## Consequences

- nstream acquires a **visible refusal** where it previously always played something. That is the
  point: `no_result` with the years actually on offer is strictly better than casting _Cash
  Truck_. Callers of the skill must handle `no_result` carrying a `years` array.
- `selection` gains a third value, `"year"`. It stays agent-facing and non-branching; the skill
  doc (`skills/nstream/SKILL.md:178`) must list it.
- A range `releaseInfo` now matches any year inside it, so `--year 2008` on a 2006-2010 series
  resolves rather than refusing. Intended.
- The soft inferred year now filters the whole result set rather than only `exact`, so
  `"dune 2021"` can select a differently-named meta. Bounded by `hits or unknown or metas` — the
  pool can never be emptied by an inference.
- Obligation on future code: a new headless selection path must run the year filter before the
  name test, not after. A path that name-matches first re-introduces exactly this bug.
- Not fixed here, and deliberately: nstream still cannot match a localized title by name. Without
  `--year`, an Italian query remains a guess. This ADR makes that guess _declarable_, not
  correct.

## References

- ADR 0021 — the same shape: a per-invocation constraint that must hold on every selection path,
  with the corollary spelled out so a new path cannot quietly opt out.
- ADR 0028 — the honesty stance this one inherits: refuse on proven evidence, grant the benefit
  of the doubt when the evidence is merely absent.
- `src/nstream/headless.py:61-89` (`_norm_title`, `_select_meta`), `:163-169` (caller);
  `src/nstream/api.py:69-81` (`_norm_text`), `:84-92` (`_search_score`);
  `src/nstream/types.py:13-29` (`Meta`); `src/nstream/cli_args.py:102` (`--year`);
  `src/nstream/headless_play.py:377` (`selection` emission).
- Field evidence: session 2026-08-08, `--year 2006 "Le vite degli altri"` → `tt0347330` (2004).
