# 0048. Genre and skip extras for unlocked addon catalogs

- **Status:** Proposed
- **Date:** 2026-10-05
- **Deciders:** Odroid/CoS planning

## Context

ADR 0046 lists catalogs from unlocked / configured addon manifests on the Film / Serie
board. A catalog that requires an extra other than `skip` is not a board row. Cinemeta
**Generi…** already exists (`cli.run_genre` → typed `top` + `api.GENRES`). `cli.run_browse`
already paginates with a trailing «altri…» row and forwards `genre` / `skip` through
`api._extras` → `api.catalog`. Addon catalogs that declare `genre` (especially
`isRequired`) never get a picker, and required-genre catalogs stay hidden. Trakt is out
of scope. ADR 0047 (`api.translate_id` before streams) stays intact.

## Decision

nstream treats `genre` and `skip` as board-supported extras for unlocked addon catalogs.
Search (and any other required extra) stays off the board. No marketplace.

1. **Browsable.** `addons._BOARD_REQUIRED_EXTRAS` is `{skip, genre}`.
   `addons.catalog_extra_info` reads the extras (and genre `options`) from the same
   unlocked manifest that contributed the board row. Same inclusion rule as
   `addons.extra_catalogs` (first catalog id wins).
2. **Genre.** Selecting an addon catalog that declares `genre` opens a picker inside
   `cli.run_browse` before the first fetch. Options come from the manifest; if omitted,
   `api.GENRES` (stable English tokens, as Cinemeta). Optional genre includes **Tutti i
   generi** (no `genre=` segment). Required genre has no Tutti; ESC returns to the board.
   Cinemeta **Generi…** is unchanged.
3. **Skip.** «altri…» is shown when the catalog is a Cinemeta pin (`catalog_extra_info`
   is `None`) or the addon declares `skip`. Addon catalogs that do not declare `skip`
   do not page. Fetch stays `api.catalog` / `api._extras`.
4. **No new surface.** No config key, CLI flag, host, indexer, or debrid probe on the
   board. Streams stay `api.streams` after ADR 0047.

## Rationale

| Option | Verdict |
| --- | --- |
| Reuse `run_browse` + extras already on the manifest | **Chosen** — `api._extras` already speaks the protocol |
| Hard-code `api.GENRES` for every addon | Insufficient — addons declare their own `options` |
| New catalog UI / marketplace | Rejected — ADR 0046 / 0024 |
| Keep required-genre catalogs off the board | Rejected — this is the deferred 0046 slice |
| Always show «altri…» | Rejected — page only when `skip` is declared (Cinemeta pins keep today's paging) |
| Trakt watch-history catalogs | Later — not this slice |

## Consequences

- Film / Serie **cataloghi addon** rows include catalogs that require `genre` (and/or
  `skip`). Search-only catalogs stay hidden.
- Users can filter and page an unlocked addon catalog when its manifest says so.
  Italian chrome; genre tokens stay as the addon declared them.
- Cinemeta **Generi…**, remux defaults, cast volume (ADR 0045), and `translate_id`
  (ADR 0047) are unchanged.
- **Not shipped here:** marketplace, Trakt, new indexers, debrid probe on the board,
  new config / `--browse` keywords.

## References

- ADR 0046 (board catalogs; genre/skip UI deferred), ADR 0047 (id translate; leave
  intact), ADR 0009 (typed Film / Serie), ADR 0024 (user manifests, no marketplace).
- Symbols: `addons.CatalogExtraInfo`, `addons.catalog_extra_info`,
  `addons._BOARD_REQUIRED_EXTRAS`, `addons.extra_catalogs`, `cli.run_browse`,
  `cli.run_genre`, `api._extras`, `api.catalog`, `api.GENRES`.
