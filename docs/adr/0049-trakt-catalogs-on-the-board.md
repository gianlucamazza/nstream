# 0049. Trakt catalogs on the board (not an indexer, not history sync)

- **Status:** Proposed
- **Date:** 2026-10-05
- **Deciders:** Odroid/CoS planning

## Context

In-repo Trakt intent is **watch-history / lists catalogs**, not a stream indexer.
`docs/roadmap.md` lists "Trakt (or similar) watch-history sync" as a product idea.
ADR 0046 deferred "Trakt sync"; ADR 0048 deferred "Trakt watch-history catalogs".
Local continue-watching (`state.resumable` / `history.json`) and the local watchlist
(`state.watchlist` / `library.json`) already exist and stay local.

The community Trakt Tv catalog addon (`catalogs[].type` = `trakt`, `meta` only with
`idPrefixes: ["trakt:"]`) is the shape that actually ships. ADR 0046 / 0048 list
unlocked manifests, but `_section_lists` and `api._catalog_addon_tasks` require
`addons.serves(..., "catalog")`. That addon never declares a `catalog` resource, so
its Popular / Trending / (once configured) Watchlist / History / lists rows never
appear and never fetch. Search-required catalogs stay off the board (ADR 0048).

A configured manifest URL embeds the user's Trakt session. Secrets stay in
`config.json` (chmod 600) or env. No marketplace, no hardcoded catalog host, no
new indexer, no debrid probe, no remux / cast-volume change (ADR 0045–0048 stay
closed except hanging this surface on `run_browse` / `api.catalog` / `play_meta`).

## Decision

nstream treats Trakt as a **user-configured catalog addon** on the existing Film /
Serie board. It is not a stream source and not a sync of local history.

1. **Not an indexer.** Trakt never contributes `stream`. Discovery stays Torrentio
   + `cfg.addons` stream addons (ADR 0024). No Trakt row in `sources.STREAM_PRESETS`.
2. **Not a sync.** No scrobble, no write to `history.json` from Trakt, no merge into
   `state.resumable` or the local watchlist. Trakt rows are board catalogs only.
3. **Source.** A Trakt catalog addon the user already unlocked: paste into Fonti
   (`cfg.addons`) and/or `cfg.trakt_addon` / `NSTREAM_TRAKT_ADDON` (env wins).
   nstream never fetches a store and never hard-codes a Trakt host.
4. **Board.** `addons.trakt_catalogs` lists board-ok catalogs (`skip` / `genre` only;
   search stays off — ADR 0048) under **── Trakt ──**. Type `trakt` lands on Film or
   Serie from the catalog id/name (`movie`/`movies` vs `series`/`show`/`shows`);
   unknown (merged watchlist, a list) appears in both. `extra_catalogs` skips Trakt
   addons so they are not duplicated under **── cataloghi addon ──**.
5. **Fetch / play.** `addons.can_fetch_catalog` is true when `serves(..., "catalog")`
   **or** the addon is a Trakt catalog addon whose `catalogs[]` declared the id.
   `api.catalog` GETs `/catalog/{declared_type}/{id}` (usually `trakt`). Play stays
   `cli.run_browse` → `play_meta` → `api.translate_id` (ADR 0047) → `api.streams`.
6. **Secrets.** Token-bearing URLs live only in config / env. Logs keep `what=`
   labels; `log.RedactFormatter` already replaces URLs. No token in the repo.

## Rationale

| Option | Verdict |
| --- | --- |
| Hang Trakt catalogs on `run_browse` / `api.catalog` | **Chosen** — 0046–0048 surface; only the `serves(catalog)` gap is Trakt-shaped |
| Native Trakt HTTP client + OAuth | Rejected — new host, new protocol, not the unlock path |
| Bidirectional history sync / scrobble | Later — roadmap idea; not this catalog slice |
| Merge Trakt continue-watching into `state.resumable` | Rejected — would mix remote lists with local resume |
| Treat Trakt as a stream indexer / preset | Rejected — in-repo intent is catalogs; no `stream` resource |
| Marketplace / hard-coded Trakt host | Rejected — ADR 0024 / 0046 |
| Reopen `_BOARD_REQUIRED_EXTRAS` for `lastVideosIds` | Rejected — this addon's user lists do not require it |
| Leave 0046 `serves(catalog)` as-is | Insufficient — the shipping Trakt addon never lists |

## Consequences

- Film / Serie show a **── Trakt ──** group when a Trakt catalog addon is configured.
  Italian chrome; manifest catalog names as-is.
- Popular / Trending work without login. Watchlist / history / personal lists appear
  only after the user pastes a configured manifest (token in the URL).
- `trakt:` catalog ids still go through ADR 0047 before streams.
- One new config key (`trakt_addon`) plus env `NSTREAM_TRAKT_ADDON`. No CLI flag,
  no `--browse` keyword, no remux / volume change.
- **Not shipped here:** marketplace, native Trakt API, scrobble, merge with local
  history/watchlist, new indexers, debrid probe on the board.

## References

- ADR 0046 (board catalogs), ADR 0047 (id translate; leave intact), ADR 0048
  (genre/skip; leave extras as `{skip, genre}`), ADR 0024 (no marketplace),
  ADR 0009 (typed Film / Serie).
- Symbols: `addons.is_trakt_catalog_addon`, `addons.trakt_catalogs`,
  `addons.trakt_board_section`, `addons.can_fetch_catalog`, `addons.effective_addons`,
  `api.catalog`, `cli.run_section`, `cli.run_browse`, `cli.play_meta`,
  `config.Config.trakt_addon`.
