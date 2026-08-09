# 0026. Stream metadata: protocol structured fields before free text

- **Status:** Accepted
- **Date:** 2026-08-01
- **Deciders:** project maintainer

## Context

`quality._text` builds the free-text corpus from which almost every heuristic is extracted —
resolution, codec, HDR/DV, size, languages, source, audio, seeders. Historically that corpus
was effectively `name + title`.

The Stremio protocol marks `title` **deprecated** in favour of `stream.description`
(“previously `stream.title`”). Comet completed the migration: measured on 35 rows for
`tt27722375`, `description` was present 35/35 and `title` 0/35. nstream therefore read the
deprecated field and ignored the current one. On a real `[RD⚡] Comet 1080p` row:

| domain field | value in payload | parsed before this ADR |
| ------------ | ---------------- | ---------------------- |
| `release_name` | `Between The Temples (2024) [Bluray 1080p][Esp]…mkv` | `''` |
| `size_gb` | `videoSize: 9366675221` | `0.0` |
| `source` | BluRay markers in description/filename | `''` |
| `languages` | Spanish flag / `[Esp]` | empty |
| `audio` / `codec` | from filename | empty |

Field observation, not hypothesis: with `audio_langs=['ita','eng']`, selection picked a
Spanish Blu-ray because the language filter could not see the language — a silent failure.
Additionally `_title_matches` with empty `release_name` grants benefit of the doubt, so the
wrong-title guard was off.

Pre-existing inconsistency: `api._dedup_by_release` collapses on `behaviorHints.filename`,
while `quality` dedup used `release_name` from free text — two keys for one notion.

## Decision

### 1. Per-field precedence: structured → text

| field | 1st source (structured) | degrade |
| ----- | ----------------------- | ------- |
| `release_name` | `behaviorHints.filename` | first line of `description`, then `title` |
| `size_gb` | `behaviorHints.videoSize` | size regex on the text corpus |
| `container` | filename extension | URL path tail (unchanged) |

Other fields (resolution, codec, HDR/DV, audio, languages, source, seeders) remain
heuristic: the protocol does not model them.

### 2. Text corpus is a union

`quality._text` is `name + description + title + filename`. `title` stays for Torrentio
(protocol says “soon”, not “removed”). Union, not per-addon branching.

### 3. Provider-neutral degrade chain

One chain for all addons. Every link degrades when the previous is missing (`behaviorHints`
absent on some rows).

### 4. Out of scope

`behaviorHints.videoHash`, `bingeGroup`, `sources` (trackers/DHT) are real opportunities but
touch other ADRs; not decided here.

## Rationale

- **`videoSize` is exact**; the emoji/size regex is approximate — remux/cast size guards need
  the integer when present.
- **`filename` is the canonical release identity** (SDK guidance); free-text headlines are
  addon-composed prose.
- **Explicit degrade** prevents half-empty, half-plausible `StreamInfo`.

## Consequences

- Behaviour changes on **Torrentio too**, not only Comet: `release_name` prefers filename;
  where it diverges from the old title headline, dedup and `_title_matches` change (intended
  — aligns quality dedup with `api._dedup_by_release`).
- `videoSize` is the **video file** byte count; text size may describe the whole torrent
  (multi-file extras).
- Parse cache keys must include `description` and the structured hints read, or distinct rows
  collapse.
- Filename in the language corpus widens false-positive surface; word-boundary `_LANG_RE`
  remains the mitigation.
- Non-empty `release_name` on Comet re-enables title match and cross-tracker dedup.

## References

- Stremio Addon SDK — Stream object:
  <https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/api/responses/stream.md>
- `quality._text`, `quality._release_name`, `quality.parse_stream`, `quality._title_matches`,
  `api._dedup_by_release`
- ADR 0024 (multi-source discovery)
