# Stream & audio selection — how nstream decides

Torrentio returns ~150 streams per title (sorted by `qualitysize`, so the first is the
biggest/most extreme file — often an 8K upscale or a 60–100 GB Dolby Vision remux that an
integrated GPU can't play). nstream parses, filters and ranks them to auto-pick the best
playable one. This document is the reference for *why* a given file/audio is chosen.

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

### 1. Parse (`quality.parse_stream` → `StreamInfo`)

Regex over `name`+`title`: `resolution`, `codec` (av1/hevc/h264), `hdr`, `dv`/`dv_profile`,
`size_gb`, `seeders`, `cached` (`[RD+]`/`[AD+]`/… provider-agnostic marker), `languages`
(ISO tokens + flag emoji; empty = **untagged**), `source`
(remux/bluray/webdl/webrip/hdtv/dvd/cam/ts/tc/scr), `audio` (headline codec, lossless first).

### 2. Filter (`unsupported_reason`, first match excludes)

Order: **hardware** (resolution cap → codec not HW-decodable → Dolby Vision P5) → **cast
audio** (TrueHD/DTS/DTS-HD, and remux, when casting) → **camrip** → **language** → **low
seeders**. Hardware checks always apply; the rest are opt-in config knobs (and
`allow_software`/`allow_dv5` can flip a hardware exclusion back to playable).

Language filter only excludes a stream **tagged exclusively with non-preferred languages**.
**Untagged streams are never excluded** (they usually carry the common audio, and most good
web releases are untagged) — see the trade-off below.

### 3. Score (`quality.score_components` / `_score`, highest precedence first)

```
(cached, resolution, lang, source, hevc, seeders_bucketed, -size)
```

| term | meaning |
|------|---------|
| `cached` | instant debrid stream (`[RD+]`) ranks first — zero wait beats raw quality |
| `resolution` | higher wins |
| `lang` | 2 = tagged with a preferred language (or `multi`), 1 = untagged, 0 = non-preferred only. Neutral (1) with no `audio_langs`. Makes the picked file likely to *contain* the wanted track |
| `source` | remux(6) > bluray(5) > webdl(4) > *unknown*(3) > webrip(2) > hdtv/dvd(1) > camrip(0) |
| `hevc` | HEVC over H.264 at equal source |
| `seeders` | capped at 40 (`_SEED_BUCKET`) so popularity doesn't force a huge file |
| `-size` | smaller file = faster streaming start, among equals |

`cached` and `resolution` stay dominant, so language/source only break ties **below** them
(no surprising resolution downgrade). See `quality.score_components` — `--explain` renders
exactly these terms per stream.

### 4. Cap & pick (`stream_select._pick_stream`)

`auto` → `playable[0]`. Manual → fzf menu capped at `max_streams` with a "show all". With
`hw_filter` off, ranking is skipped entirely (Torrentio order).

## Config knobs

`hw_filter` (master switch), `max_resolution`, `allow_software`, `allow_dv5`, `lang_filter`,
`audio_langs` (preference order — drives the `lang` score term and `--alang`),
`exclude_camrip`, `min_seeders`, `dedup`, `max_streams`. Editable via `nstream --settings`.

## Languages: one registry, one allow-list

All language knowledge — release-name tokens, flag emoji, display names — lives in one place,
`languages.py` (`LANGUAGES`). `quality` derives its token/flag maps from it and `caster` its
names, so adding a language is a one-line change (no drift across modules).

The selected languages are a **single ordered allow-list**: `audio_langs` / `subtitle_langs`
in config. They drive `--alang`/`--slang`, the `lang` score term, and the soft `lang_filter`
demotion — there is deliberately **no separate "hard view filter"** concept (it would risk
hiding untagged/multi releases that usually carry the wanted audio). Edit them in
`nstream --settings` → *Lingue audio/sottotitoli*, a multi-select (TAB to toggle, Enter to
confirm) built from the registry; order is preserved (selected-first) so `--alang` priority
is kept. `lang_filter` remains the one knob that governs whether non-preferred tagged streams
are demoted.

## Audio: stream language vs track language

A stream is a whole file with embedded audio tracks. `StreamInfo.languages` is a **heuristic
guess** from the release name. The **ground truth** is the ISO tags ffprobe reads from the
container. They can disagree (a release tagged `ITA.ENG` may actually hold ita/eng/fra).

- **Local (mpv):** nstream injects `--alang=<audio_langs>` (unless you set `alang` in
  `mpv.conf`/`mpv_args`). mpv picks the first track in the first preferred language present.
  Manual mode (`choose_tracks`) lets you pick an exact track by `--aid` from the ffprobe list.
  **Auto-play guard** (`stream_select.prepare_stream`): before playing, if the auto-pick isn't
  tagged with a preferred language, nstream ffprobes it; when no track matches `audio_langs` it
  warns and (interactively) reopens the stream menu instead of letting mpv silently fall back to
  the wrong dub. When only a fallback language is present it plays it with primary-language safety
  subtitles. Tags are matched
  through `languages.normalize` so a 2-letter container tag (`it`) matches a 3-letter pref
  (`ita`). Best-effort: a missing/failed probe never blocks playback; binge advances warn and
  continue. Well-tagged preferred releases skip the probe.
- **Cast (Chromecast):** the receiver plays the file's **default** track and cannot switch
  embedded tracks. The in-cast `a` hotkey re-casts a *different* release tagged in the chosen
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
