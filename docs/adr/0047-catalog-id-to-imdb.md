# 0047. Translate catalog ids to IMDb before stream discovery

- **Status:** Accepted
- **Date:** 2026-10-05
- **Deciders:** Odroid/CoS planning

## Context

ADR 0046 lists addon catalogs on the Film / Serie board and uses a `tt` **already on the
row** (`api.play_id` reads `imdb_id` / `behaviorHints.defaultVideoId`). That is not a
translator. Catalog rows that only carry `tmdb:`, `kitsu:`, or a similar non-IMDb id stay
as-is. Stream addons (`addons.serves`) speak `tt` (Torrentio also `kitsu`). `api.streams`
then fans out to nothing for a bare `tmdb:` id, so those board rows cannot play.

Cinemeta's built-in meta resource is `tt` only. The same unlock path that added the catalog
already exposes that addon's `/meta/{type}/{id}.json` (token-free). `api.meta` /
`api.meta_cached_disk` already fetch it. No new indexer, marketplace, or debrid probe.

## Decision

nstream resolves a non-IMDb catalog id to an IMDb `tt…` **before** stream discovery
(`api.streams`) and series episode lists (`api.episodes`), so `prepare_stream` and Torrentio
see a `tt`.

1. **`api.translate_id`.** `tt…` (including `tt…:season:episode`) passes through. Otherwise
   a meta lookup on addons that already serve that prefix reads `imdb_id` or
   `behaviorHints.defaultVideoId`. The existing in-process + disk meta cache is used. An
   episode suffix on the catalog id is reattached to the `tt` root (`tmdb:9:1:2` →
   `tt77:1:2`).
2. **Gates.** `api.streams` and `api.episodes` call `translate_id` first. TUI `cli.play_meta`
   / `cli._play_video` and headless title pick rewrite the id so history and `--json`
   `imdb_id` report the `tt`.
3. **Fail soft.** No fabricated `tt`. If meta has no IMDb id and a stream addon already
   serves the original prefix (Torrentio + `kitsu`), the original id is kept. If neither a
   `tt` nor a streamable prefix exists, raise `api.IdUntranslated` — TUI notice and
   `--json` `error: id_untranslated`.

## Rationale

| Option | Verdict |
| --- | --- |
| Keep using fields already on the catalog row (0046) | Insufficient — many rows have no `tt` |
| New mapping service / hardcoded TMDB-IMDb index | Rejected — new host, tokens, or a baked index |
| Cinemeta name search as the translator | Rejected — can invent the wrong `tt` |
| Translate only on the TUI board path | Rejected — `--json`, history, explain/probe share `api.streams` |
| Require a `tt` even when Torrentio serves `kitsu` | Rejected — would break anime rows that already play |
| Ignore prefixes and query Cinemeta with `tmdb:` | Rejected — Cinemeta `idPrefixes` are `tt`; empty 404s |

## Consequences

- A board row whose catalog id is `tmdb:` / `kitsu:` (or similar) plays when the unlocked
  meta addon returns an `imdb_id` (or Torrentio already serves `kitsu`).
- Translation failure is a dedicated error, not an empty `no_streams` fan-out and not a
  fake `tt`.
- One extra token-free meta GET on first play of an untranslated id (cached after that).
- **Not shipped here:** marketplace, Trakt, new indexers, debrid probe on the board,
  remux/cast-volume changes (ADR 0045 / 0046 stay closed).

## References

- ADR 0046 (row-local `tt` only), ADR 0024 (user manifests, no marketplace).
- Symbols: `api.translate_id`, `api.IdUntranslated`, `api.streams`, `api.episodes`,
  `api.meta_cached_disk`, `api.play_id`, `cli.play_meta`, `cli._play_video`,
  `headless.run_auto`, `failures.describe`.
