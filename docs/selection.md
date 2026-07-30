# Stream & audio selection — how nstream decides

Stream addons (built-in **Torrentio** when `torrentio_enabled`, plus any `cfg.addons`
manifests — Comet, MediaFusion, AIOStreams, …) return on the order of ~100+ streams per
title. Torrentio sorts by `qualitysize`, so the first is often the biggest/most extreme
file (8K upscale or a 60–100 GB Dolby Vision remux that an integrated GPU can't play).
Before ranking, `api.streams` drops unplayable shapes, fuses debrid `url` with pure-torrent
`infoHash` by filename across addons, and collapses duplicate releases (cached > url >
infoHash); each row carries `addon` provenance for labels/`--explain` (ADR 0024).

nstream then parses, filters and ranks the list to auto-pick the best playable one. This
document is the reference for _why_ a given file/audio is chosen.

To see the decision for a real title live:

```
nstream "<title>" --explain
```

It prints the detected capabilities, the active filters, every stream with its score terms
and playable/excluded status, the auto-pick, and the actual audio tracks (via ffprobe) with
the one mpv would select — without playing anything.

## Pipeline

`quality.parse_stream` → `unsupported_reason` (filter) → `_score` (rank) → cap → pick
(`stream_select._pick_stream`). For a movie the auto-pick is `playable[0]`; manual mode shows an fzf
menu (top `max_streams` playable + a "show all" entry that reveals the rest and the
excluded ones, each marked with its reason).

## Title discovery

Catalog and search results are deduplicated across configured addons. Search then applies a local,
token-free presentation ranking: exact normalized title matches first, then title prefixes,
substring matches, and finally the remaining addon results. Case, accents and punctuation do not
change the comparison, while the original addon result remains the playback source of truth.
The TUI stores recent queries and a metadata-only local watchlist in
`XDG_STATE_HOME/nstream/library.json`; no stream URL or provider token is persisted there.

### 1. Parse (`quality.parse_stream` → `StreamInfo`)

Regex over `name`+`title`: `resolution`, `codec` (av1/hevc/h264), `hdr`, `dv`/`dv_profile`,
`size_gb`, `seeders`, `cached` (`[RD+]`/`[AD+]`/… provider-agnostic marker), `languages`
(ISO tokens + flag emoji; empty = **untagged**), `source`
(remux/bluray/webdl/webrip/hdtv/dvd/cam/ts/tc/scr), `audio` (headline codec, lossless first).

### 2. Filter (`unsupported_reason`, first match excludes)

Order: **hardware** (resolution cap → codec not HW-decodable → Dolby Vision P5) → **cast
audio** → **camrip** → **language** → **low seeders**. Hardware checks always apply; the rest
are opt-in config knobs (and `allow_software`/`allow_dv5` can flip a hardware exclusion back to
playable).

The **cast audio** exclusion (TrueHD/DTS/DTS-HD, and remux, when casting) only applies when
Tier-2 remux is **off** (`cfg.cast_remux = false`). With remux on (the default), those titles
are no longer excluded — the host remuxes their audio to AAC (`remux.py`) — so they're only
_ranked_ below native-AAC releases, not dropped (see the cast score below and `docs/adr/0005`).

Language filter only excludes a stream **tagged exclusively with non-preferred languages**.
**Untagged streams are never excluded** (they usually carry the common audio, and most good
web releases are untagged) — see the trade-off below.

### 3. Score (`quality.score_components` / `_score`, highest precedence first)

```
(cached, resolution, lang, source, hevc, seeders_bucketed, -size)
```

| term         | meaning                                                                                                                                                                                |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cached`     | instant debrid stream (`[RD+]`) ranks first — zero wait beats raw quality                                                                                                              |
| `resolution` | higher wins                                                                                                                                                                            |
| `lang`       | 2 = tagged with a preferred language (or `multi`), 1 = untagged, 0 = non-preferred only. Neutral (1) with no `audio_langs`. Makes the picked file likely to _contain_ the wanted track |
| `source`     | remux(6) > bluray(5) > webdl(4) > _unknown_(3) > webrip(2) > hdtv/dvd(1) > camrip(0)                                                                                                   |
| `hevc`       | HEVC over H.264 at equal source                                                                                                                                                        |
| `seeders`    | capped at 40 (`_SEED_BUCKET`) so popularity doesn't force a huge file                                                                                                                  |
| `-size`      | smaller file = faster streaming start, among equals                                                                                                                                    |

`cached` and `resolution` stay dominant, so language/source only break ties **below** them
(no surprising resolution downgrade). See `quality.score_components` — `--explain` renders
exactly these terms per stream.

**Cast score** (`cast=True`, models the Default Media Receiver, not the GPU):

```
(cached, remux_within_size, cast_audio, remux_within_cap, resolution, lang, source, cast_h264, seeders_bucketed, -size)
```

| term                | meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `remux_within_size` | demotes a **likely-remux** release bigger than `cast_remux_max_size_gb` (default 20) below any feasible alternative — size is the real download cost. A _likely-remux_ is Dolby/DTS audio **or** an unlabelled REMUX (its name omits the codec but it carries the lossless disc track, so it reads as decodable/unknown yet really needs a huge remux — the resolution cap can't see it). Ranked right after `cached`: avoiding a pick the cast-time size guard would reject matters more than codec/resolution. AAC releases never trip it (no remux, streamed directly even at 4K). A preference, not an exclusion |
| `cast_audio`        | 2 = receiver decodes it natively (AAC/Opus/FLAC…), 1 = untagged, 0 = Dolby/DTS (needs a Tier-2 remux). Prefers AAC so the **direct, instant** cast wins and a remux (a prepare wait) only triggers when no AAC release exists                                                                                                                                                                                                                                                                                                                                                                                        |
| `remux_within_cap`  | among releases that need a remux, prefers those ≤ `cast_remux_max_resolution` (default 1080p) — a remux downloads the whole file, so a 4K Dolby release is a 30-60 GB fetch while a direct 4K cast is free. A preference, not an exclusion                                                                                                                                                                                                                                                                                                                                                                           |
| `cast_h264`         | tie-breaker only (the receiver decodes HEVC natively too)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |

The receiver plays HEVC/4K/HDR natively, so after audio the **resolution** wins; H.264-vs-HEVC
no longer matters. Native-AAC titles are never capped (they cast direct, no download).

After ranking, the cast pick passes through `stream_select.vet_cast_audio`, which **enforces the
audio language**: the Default Media Receiver plays a file's first track and can't switch embedded
audio tracks (Google Cast: only _text_ tracks are selectable without a custom receiver), so it
ffprobes the dub and either casts directly (first track already primary + decodable), remuxes to
keep only the primary-language track (ffmpeg `0:a:N`), reselects another dub, or casts with
primary-language safety subtitles. This is why a high-ranked but wrong-language pick still ends up
in the right language.

### 4. Quality choice (before pick)

Per-session **exact resolution** filter (`PlayOpts.quality` / `--quality`):

| Value                       | Meaning                                                                                       |
| --------------------------- | --------------------------------------------------------------------------------------------- |
| `None`                      | Undecided: TUI shows an in-flow fzf picker (Auto + resolutions present); headless = no filter |
| `0`                         | Auto (no exact filter; binge sticky so the picker is not re-shown)                            |
| `720` / `1080` / `2160` / … | Hard-filter: only streams with `StreamInfo.resolution == N`                                   |

Applied in `unsupported_reason` **after** the hardware `max_resolution` cap and **before**
cast-audio/camrip/lang. Unknown resolution (`0`) is excluded when the filter is active.
Distinct from `max_resolution` (GPU safety ceiling): quality is a session preference, not a
hardware limit. Ranking inside the filtered set is unchanged (`cached` still dominates).

CLI: `nstream --quality 1080 "…"`, `nstream --json --quality 4k --cast "…"`. Aliases:
`auto`, `4k`/`uhd`/`2160`, `fhd`/`1080`, `hd`/`720`, `sd`/`480`. Headless failure:
`error: quality_unavailable` with `available_resolutions`. Series binge sticky-propagates
the first episode's choice via `VettedStream.quality`.

### 5. Cap & pick (`stream_select._pick_stream`)

`auto` → `playable[0]`. Manual → fzf menu capped at `max_streams` with a "show all". With
`hw_filter` off, ranking is skipped entirely (Torrentio order), but an exact quality filter
still subsets the list first.

## Availability: the cached marker is a guess (ADR 0014 + 0025)

`[RD+]`/`[TB+]` come from a crowdsourced database, not from the provider (Real-Debrid removed
its cache endpoint in 2024 — ADR 0002), so a "cached" release can be evicted, expired, or
**removed** (DMCA) while still advertised. Two guards, both auto-pick only:

1. **Pre-commit verification** (`_verify_availability`): the top 5 url-ready candidates are
   probed concurrently before anything is committed — _all_ of them, cached or not, since the
   seeder count that gates uncached rows describes swarm health, not debrid availability.
2. **Last-resort fallback** (`_ensure_playable`): the committed pick is re-checked (memoized,
   so no double probe) and falls back to local P2P (hybrid stream) or the next candidate.

The probe (`net.probe_url`) is **classified, not boolean**, because an HTTP 200 proves the url
resolves, not that the content is playable — the server may be serving a file that is still
arriving, or a placeholder left where the content used to be. Each signal decides only what it
can actually prove:

| Verdict   | Signal                                                                                       | Effect                                   |
| --------- | -------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `live`    | 2xx with a plausible total size                                                              | keep                                     |
| `gone`    | 404/410/4xx — the resource isn't there                                                       | drop **and** denylist                    |
| `unknown` | served total << announced (incomplete/in flight), 403/405/416, 5xx, timeout, transport error | drop for this run — **never** denylisted |

The size check never escalates to `gone`: it cannot tell a transfer still in flight (Torrentio's
`[RD download]`) from an emptied file, and that distinction is the whole of the "removed"
inference. It stays exactly as useful for what it does prove — this file is not playable now.

A `gone` verdict is persisted to `XDG_STATE_HOME/nstream/dead-sources.json` (key: infoHash →
filename → name; TTL 30 days, 500 entries max) and applied as a **pre-ranking filter**
(`prune_dead`), so a removed release stops costing a probe on every search and never reaches
the picker. `nstream --forget-dead` clears the list. Headless answers `error: sources_removed`
(with `removed_sources`) only when the denylist accounts for every candidate — an empty set of
merely-not-ready sources is `no_playable_stream`, which is the truth.

## Config knobs

`hw_filter` (master switch), `max_resolution`, `allow_software`, `allow_dv5`, `lang_filter`,
`audio_langs` (preference order — drives the `lang` score term and `--alang`),
`exclude_camrip`, `min_seeders`, `dedup`, `max_streams`. Editable via `nstream --settings`.
Per-invocation quality is **not** a config default (v1): use `--quality` or the TUI picker.

## Languages: one registry, one allow-list

All language knowledge — release-name tokens, flag emoji, display names — lives in one place,
`languages.py` (`LANGUAGES`). `quality` derives its token/flag maps from it and `caster` its
names, so adding a language is a one-line change (no drift across modules).

The selected languages are a **single ordered allow-list**: `audio_langs` / `subtitle_langs`
in config. They drive `--alang`/`--slang`, the `lang` score term, and the soft `lang_filter`
demotion — there is deliberately **no separate "hard view filter"** concept (it would risk
hiding untagged/multi releases that usually carry the wanted audio). Edit them in
`nstream --settings` → _Lingue audio/sottotitoli_, a multi-select (TAB to toggle, Enter to
confirm) built from the registry; order is preserved (selected-first) so `--alang` priority
is kept. `lang_filter` remains the one knob that governs whether non-preferred tagged streams
are demoted.

## Audio: stream language vs track language

A stream is a whole file with embedded audio tracks. `StreamInfo.languages` is a **heuristic
guess** from the release name. The **ground truth** is the ISO tags ffprobe reads from the
container. They can disagree (a release tagged `ITA.ENG` may actually hold ita/eng/fra).

- **Local (mpv):** nstream injects `--alang=<audio_langs>` (unless you set `alang` in
  `mpv.conf`/`mpv_args`). mpv picks the first track in the first preferred language present.
  Manual mode (`subs.choose_tracks`) lets you pick an exact track by `--aid` from the ffprobe list.
  **Auto-play guard** (`stream_select.prepare_stream`): before playing, if the auto-pick isn't
  tagged with a preferred language, nstream ffprobes it; when no track matches `audio_langs` it
  warns and (interactively) reopens the stream menu instead of letting mpv silently fall back to
  the wrong dub. When only a fallback language is present it plays it with primary-language safety
  subtitles. Tags are matched
  through `languages.normalize` so a 2-letter container tag (`it`) matches a 3-letter pref
  (`ita`). Best-effort: a missing/failed probe never blocks playback; binge advances warn and
  continue. Well-tagged preferred releases skip the probe.
- **Cast (Chromecast):** the receiver plays the file's **default** track and cannot switch
  embedded tracks. The in-cast `a` hotkey re-casts a _different_ release tagged in the chosen
  language; it can only pick a file whose tag matches, not force a track, so a file whose
  default track isn't that language may still play the wrong audio (best-effort).

This stream-language/track-language split is the usual cause of "wrong audio": if an untagged
English-only release wins on quality and has no Italian track, `--alang=ita,eng` falls back.
The `lang` score term mitigates this by preferring a file tagged with your language.

## Deliberate trade-offs

- **`cached` dominates** language and resolution: a cached 1080p outranks a non-cached 4K /
  a non-cached preferred-language file. Instant playback is intentionally the top priority.
- **Untagged streams pass the language filter** (benefit of the doubt) but rank below tagged
  preferred-language ones via the `lang` score term.
- **Cast can't select tracks** → language switching is per-file, not per-track.
