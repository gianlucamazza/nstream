# 0046. Board catalogs from unlocked addon manifests (not a marketplace)

- **Status:** Proposed
- **Date:** 2026-10-05
- **Deciders:** Odroid/CoS planning

## Context

The refresh/unlock path (`scripts/refresh-stream-addons.py`, settings → Fonti) already
writes user-pasted manifest URLs into `cfg.addons`. Those manifests declare `catalog`
resources. The TUI board (`cli.run_section` / `cli._home_menu`) only pins the three
Cinemeta ids (`top` / `year` / `imdbRating`). `addons.extra_catalogs` already exists but
drops every catalog whose type is not `movie`/`series` (Anime Kitsu is `anime`), and
`api.catalog` always queries built-in Cinemeta for any id. Catalog rows from The Movie
Database Addon carry `tmdb:` ids plus an `imdb_id` already on the row; stream addons
speak `tt` / `kitsu` (`addons.serves`). A marketplace, Trakt sync, a general id
translator, and new genre/skip extras are not in this slice.

## Decision

nstream browses catalogs declared on manifests of addons the user has already unlocked
or configured. The typed Film / Serie TV board is the surface. No marketplace.

1. **Source.** `addons.extra_catalogs` reads `effective_addons` (built-ins + `cfg.addons`).
   Catalogs come only from those manifests. nstream never fetches a store, never ships
   third-party tokens, and never hard-codes a catalog host into the runtime.
2. **Board.** Typed sections list extra catalogs under **── cataloghi addon ──**.
   Cinemeta-shaped (`movie` / `series`) rows stay in their section. A catalog whose
   type is not a board type (today: `anime`) appears in both sections. Selecting a
   row calls the existing `cli.run_browse` → `api.catalog` → `cli.play_meta` path.
3. **Fetch.** `addons.catalog_fetch_type` picks the path type: the section type when
   the addon declares that pair, otherwise the addon's own type for that id. Built-ins
   are queried only for catalogs they declare. Stdlib HTTP via `net.http_get_json` /
   `api._cached_json`.
4. **Play id.** `api.play_id` uses a `tt` id already on the row (`imdb_id` or
   `behaviorHints.defaultVideoId`) when the catalog id is not streamable. That is not
   a translator: no second resolver, no new index. Streams stay `api.streams`.
5. **Browsable.** A catalog that requires an extra other than `skip` (search, …) is
   not a board row. Genre / skip UI for addon catalogs is later.

## Rationale

| Option | Verdict |
| --- | --- |
| Marketplace / Stremio addons.net browse | Rejected — non-goal; tokens; not the unlock path |
| New catalog subsystem | Rejected — `extra_catalogs` / `api.catalog` / `run_browse` already exist |
| Film/Serie only, drop `anime` catalogs | Rejected — unlock addons already declare them; items are `movie`/`series` |
| New Anime home section | Deferred — extra depth; both typed sections are enough for this slice |
| General tmdb↔tt / kitsu↔tt translator | **ADR 0047** — only use fields already on the catalog row here |
| Genre / skip extras for addon catalogs | Later — Cinemeta Generi… stays; addon extras are not a new UI |

## Consequences

- Film / Serie grow a **cataloghi addon** group when `cfg.addons` manifests declare
  browsable catalogs. Labels are `Addon · name` (Italian chrome, manifest name as-is).
- `api.catalog` may GET `/catalog/anime/…` from a Film/Serie pick, then keep rows whose
  `type` matches the section.
- Rows with only a `tmdb:` id and no `imdb_id` stay as-is; stream fan-out may be empty
  until ADR 0047.
- No new config key, CLI flag, dependency, or debrid probe on the board.
- **Not shipped here:** marketplace, Trakt, id translation, addon genre/skip extras,
  new indexers.

## References

- ADR 0009 (typed Film / Serie sections), ADR 0024 (user manifests, no marketplace).
- Symbols: `addons.extra_catalogs`, `addons.catalog_fetch_type`, `addons.effective_addons`,
  `api.catalog`, `api.play_id`, `cli.run_section`, `cli.run_browse`, `cli.play_meta`.
- Unlock path: `scripts/refresh-stream-addons.py` (`CATALOG_ADDONS`), settings Fonti.
