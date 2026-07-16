---
name: nstream
description: >
  Play or cast movies and TV series headlessly via the nstream CLI. Use when the user wants to
  watch / put on / stream a specific film or series — locally on this laptop (mpv) or on the TV
  (Chromecast), and to stop/check/set-volume on the cast it started. Always non-interactive:
  drives `nstream --json` (no fzf, no TTY) and parses the JSON result. For mirroring the desktop
  or casting an arbitrary file/URL use the `skill-cast` skill instead. Trigger keywords: metti sul TV,
  guarda, riproduci, streaming film, casta il film, metti la serie, guarda l'episodio, mettimi un
  film, put on the TV, watch, play movie, stream series, cast the movie, watch the episode.
---

# nstream — headless play / cast for movies & series

Drive the local `nstream` CLI in its **headless** mode (`--json`) to search a title and play it
locally or cast it to the TV, without any interactive picker. Every command prints **one JSON
object on stdout**; parse it and confirm the outcome to the user.

## Before anything

- `command -v nstream` — if missing, tell the user to run `./install.sh` in
  `~/Workspace/tooling/nstream` (installs the CLI via `uv tool`).
- Confirm headless support once: `nstream --help | grep -q -- --json`. If absent, the installed
  CLI predates headless mode — tell the user to reinstall from the repo.

## Intent → command

Always pass `--json`. Quote the title.

| Intent                               | Command                                                                                                                     |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| "metti X sul TV" / "casta X"         | `nstream --json --cast "X"`                                                                                                 |
| "guarda X" / "riproduci X in locale" | `nstream --json --local "X"`                                                                                                |
| cast via realtime mirror (1080p SDR) | `nstream --json --mirror "X"` (instant start, no remux download; not desktop mirror — use `skill-cast` for that)             |
| disambiguate by year                 | `nstream --json --cast --year 1999 "X"`                                                                                     |
| only movies / only series            | add `--movies` or `--series` (search/browse/continue; avoids same-named title of the other type)                           |
| a specific series episode            | `nstream --json --cast --season 1 --episode 3 "X"`                                                                          |
| target a specific TV                 | `nstream --json --cast --device "Salotto" "X"`                                                                              |
| with subtitles                       | add `--subs` (preferred lang) or `--sub-lang ita`                                                                           |
| subs out of sync: constant shift     | add `--sub-offset -2.5` (seconds, ±; retimes the file → works for mpv AND cast)                                             |
| subs out of sync: progressive drift  | add `--sub-fps 25:23.976` (fps the subs were authored for : fps of the video)                                               |
| force the audio/dub language         | add `--audio-lang eng` (e.g. original audio + `--sub-lang ita`)                                                             |
| force stream quality / resolution    | add `--quality 1080` (or `720`, `4k`/`2160`, `auto`) — hard filter; fails if that res is absent                            |
| list audio/subs/resolutions (no play)| `nstream --json --probe "X"`                                                                                                |
| resume last watched                  | `nstream --json -c "X"` (or no title for the most recent)                                                                   |
| next episode after finishing one     | `nstream --json --cast -c "X"` (auto-advances when the last episode is finished; `error: series_completed` past the finale) |
| list a series' episodes              | `nstream --json --probe "X"` without `--episode` → `action: "episodes"` (add `--season N` to narrow)                        |
| why was this stream picked?          | `nstream --json --explain "X"` (ranking + filters as data; add `--cast` for the TV profile; read-only)                      |
| something popular / new / top-rated  | `nstream --json --cast --browse popolari\|nuovi\|top`                                                                       |
| stop what's casting                  | `nstream --json --stop`                                                                                                     |
| what's casting now                   | `nstream --json --status`                                                                                                   |
| pause / resume the cast              | `nstream --json --pause` / `nstream --json --resume`                                                                        |
| jump to a position                   | `nstream --json --seek 1250` (seconds)                                                                                      |
| set the TV volume (cast in progress) | `nstream --json --volume 35` (0–100, no title needed)                                                                       |
| set the start volume of a new cast   | `nstream --json --cast --volume 35 "X"`                                                                                     |

### Audio & subtitles

- **Subtitles**: `--sub-lang ita` forces Italian subs; `--subs` picks your preferred language.
  The play result reports the active `subtitles` lang. Selection is **evidence-tiered**
  (ADR 0020): a protocol hash match (OpenSubtitles moviehash of the exact file) wins outright;
  otherwise, on casts that go through the Tier-2 remux, the delivered subtitle is **aligned
  against the real audio of the remuxed file** (native engine, confidence-gated: it corrects or
  verifies when the evidence is strong and refuses honestly when it isn't); otherwise it is an
  honest language guess. `subtitles_match` reports which tier decided: `"hash"` =
  protocol-verified, `"audio"` = aligned to the local media (the applied correction is in
  `subtitles_offset`, seconds), `"lang"` = guess — correct a guess with
  `--sub-offset`/`--sub-fps` (changing them for a running cast needs a re-cast; resume makes it
  cheap). Manual flags always win: the engine steps aside.
- **Audio/dub**: `--audio-lang CODE` forces a specific dub (e.g. `eng` for original audio). If no
  stream carries that language the command fails with `error: audio_lang_unavailable` and an
  `available_audio` list — **do not** silently play another language; show the available options
  and ask the user.
- **Discover first**: when the user is unsure ("what languages / qualities does X have?"), run
  `--probe` — without playing it returns:
  - title with streams: `action: "probe"` + `available_audio`, `available_subtitles`,
    `available_resolutions`
  - series without `--episode`: `action: "episodes"` + `episodes[{season,episode,title}]`
    (optionally narrow with `--season N`)
  Present the choices, then play with `--audio-lang` / `--sub-lang` / `--quality` as needed.
- Every play result includes `audio_lang` (the dub played) and `available_audio` (what else was
  on offer), so you can confirm precisely what was started.
- **Quality**: `--quality 1080` (aliases: `4k`/`2160`, `fhd`/`1080`, `hd`/`720`, `auto`) hard-
  filters to that resolution before ranking. If none match: `error: quality_unavailable` with
  `available_resolutions` — show the list and ask (or drop the flag). Success echoes `quality`
  (requested; null/omitted when Auto) and `stream.resolution` (actual), plus
  `available_resolutions`. Without `--quality`, headless does not filter (best playable wins).
  Combined with `--audio-lang`: quality is applied first; if the res exists but no stream at
  that res carries the dub, you get `audio_lang_unavailable` (not `quality_unavailable`).
- **Track-accurate**: when `--audio-lang` is set, nstream ffprobe-confirms the chosen stream's
  REAL audio tracks carry that language before sending it (the release name can mistag). The
  result's `audio_verified` is `true` when confirmed by the actual tracks, `false`/`null` when
  unverifiable (e.g. an `und` single track — accepted on benefit of the doubt). If no stream's
  real tracks carry the language, it returns `audio_lang_unavailable` instead of sending the
  wrong dub.

### Tier-2 remux (Dolby/DTS audio → native-fidelity cast)

- The Chromecast plays HEVC/4K/HDR video natively but **can't decode AC-3/E-AC-3/DTS/TrueHD**
  (silent). nstream now **remuxes on the host** for those titles — keeps the original video
  (`-c copy`, so 4K/HDR/HEVC are preserved) and transcodes only the audio to AAC, then casts the
  complete file. The selector **prefers an AAC release first** (instant, no remux), so this only
  kicks in when every release for the title carries Dolby/DTS audio.
- Cost: a **prepare wait** — the whole file is downloaded+remuxed before playback starts (this TV
  only plays a complete file; streaming-while-transcoding doesn't work on it). Expect seconds-to-
  minutes depending on size; the CLI prints `📺 preparo l'audio per il cast…` on stderr.
- The play result adds **`reencoded: true`** when a Tier-2 remux was used (video native, audio→AAC);
  `false` for a direct cast. Mention it if the user asks why a Dolby title took a moment to start.
- To avoid a pathological fetch, the selector **caps remuxed releases to 1080p by default** (a 4K
  remux means a 30-60 GB download, while a _direct_ 4K cast streams for free): among Dolby-only
  titles it prefers a 1080p release over a 4K one. It's a preference — a sole 4K Dolby release is
  still cast — and lifts to whatever `cast_remux_max_resolution` is (0 = no cap). Native-AAC titles
  are never capped (they cast direct, no download).
- Toggle/target via config: `cast_remux` (default on), `cast_audio_codec` (default `aac`),
  `cast_remux_max_resolution` (default 1080; 0 = no cap).
- `--stop` also tears down the remux server + temp file. If a remux cast is left running and never
  stopped, the next run garbage-collects the stale temp file.

### Cast lifecycle (nstream is now self-sufficient)

- `nstream --json --status` → `player_state`, `title`, `position`, `duration`, `volume`, `muted`
  of the receiver (use for "what's playing / is it still on?"), plus `active_tracks` (the
  receiver's confirmed active track ids — a caption track shows here once it's really activated)
  and `receiver_error` (a codec/caption rejection reported by the receiver, else null). Also
  refreshes the resume point of a fire-and-return cast in the watch history (cast session).
- `nstream --json --stop` → stop the cast AND persist the receiver's position to the watch
  history, so a later `-c "titolo"` resumes where the user stopped.
- `nstream --json --cast --volume N "X"` → set the receiver volume (0–100) when starting a cast;
  combine with the `volume`/`muted`/`notice` fields the cast result already reports.
- (For mirroring the desktop or casting an arbitrary file/URL, still use the `skill-cast` skill.)

Notes:

- **Cast is fire-and-return by default** (`--no-follow` implied): the command returns as soon as
  the receiver has the media. The cast is still recorded in the watch history (a "started"
  entry + cast session): prefer `--json --stop` to end it so the position is persisted, and
  `-c "titolo"` will resume/propose correctly. Add `--follow` only if the user wants live
  resume/auto-advance tracking (it will hold the terminal for the whole runtime).
- **`--follow` streams playback events as JSONL** (one JSON object per line) instead of a single
  final object:
  `{"action":"cast","event":"started|playing|paused|ended|failed|disconnected", ...}` with
  `position`/`duration` on the playing/ended/disconnected lines. Read until `event:"ended"` or
  `event:"failed"`. **`disconnected` is not `ended`**: the castbridge daemon socket died mid-cast;
  the receiver may still be playing (Tier-2 keeps its Range server alive for `--stop`/GC). Treat
  it as an uncertain mid-session loss of telemetry, not a clean finish. Without `--follow` you
  get the usual single summary object.
- **Now-playing metadata on the TV + HUD**: when the native `castbridge` backend is built, the
  cast sends the title, poster, and season/episode so the TV's now-playing card and the desktop
  HUD widget show them — no extra flags. Without castbridge it transparently falls back to the
  metadata-less path (catt). Nothing changes in how you invoke it.
- **Local (`--local`) blocks** until the mpv window is closed and opens a window on the desktop —
  use it only when the user is physically at the machine. Prefer `--cast` otherwise. If you must
  run `--local` non-blocking, launch it in the background.
- Series default to **S01E01** when `--season`/`--episode` are omitted; headless plays exactly the
  one requested episode (no auto-binge).

## Parsing the result

stdout is always a single JSON object (except `--follow` JSONL). Read `ok`:

- `ok: true` → confirm to the user with `title`, `action` (`cast` / `play` / `probe` /
  `episodes` / `explain` / lifecycle), `device` when casting, and a short stream summary from
  `stream` when present (e.g. "1080p HEVC, audio ita, cached"). Also surface when relevant:
  - `quality` — requested filter (null = Auto / none)
  - `available_resolutions` — tiers on offer for this title
  - `audio_lang` / `available_audio` / `audio_verified`
  - `reencoded` — Tier-2 remux used
  - `selection` — `exact` name match vs `first`-result guess (if `first`, say which title)
- `ok: false` → handle by `error` code (below).

### Error codes

- `no_result` — nothing matched the title (or empty history for `-c`). Offer to retry with a
  different spelling or add a year / `--movies`/`--series`.
- `no_streams` — title found but no sources (often "not released yet"); surface `message`.
- `no_playable_stream` — sources exist but none pass the hardware/cast filters; try `--local`,
  or a lower tier with `--quality 1080` / `720`.
- `video_codec_unsupported` — the REAL (ffprobe-verified) video codec of every candidate is one
  the Chromecast can't render (e.g. a DivX/MPEG-4 ASP rip) and the mirror fallback isn't
  available — casting would show a black screen. `video_codec` carries the codec; offer
  `--local` (mpv decodes anything) or a different `--quality`.
- `video_codec_unsupported` — the REAL (ffprobe-verified) video codec of every candidate is one
  the Chromecast can't render (e.g. a DivX/MPEG-4 ASP rip) and the mirror fallback isn't
  available — casting would show a black screen. `video_codec` carries the codec; offer
  `--local` (mpv decodes anything) or a different `--quality`.
- `audio_lang_unavailable` — the requested `--audio-lang` isn't in any (remaining) stream; show
  the `available_audio` list and ask which dub to use (or drop `--audio-lang`).
- `quality_unavailable` — the requested `--quality` isn't among playable streams; show
  `available_resolutions` and ask (or drop `--quality` / try another tier).
- `episode_not_found` — show the `available` seasons/episodes and ask which to play.
- `series_completed` — `-c` / next-episode past the finale; tell the user the show is finished.
- `device_not_found` — no Chromecast resolved, or several TVs and none specified. Run `catt scan`
  to list devices; if more than one, ask the user which (AskUserQuestion) and re-run with
  `--device "<name>"`.
- `network` — addon/API network failure; surface `message` and retry later.
- `usage` — a bad flag combo (e.g. `--sub-menu` with `--json`, invalid `--quality`); fix the
  command.
- Missing debrid token / config error → nstream exits non-zero and prints to **stderr** (or JSON
  `error: config` on some paths); surface that line and point the user at `nstream --settings`.

## HDR on the external monitor (local playback only)

For `--local` playback of HDR content (4K/HDR/DV releases) on the docked external monitor
(DP-2), pin the output to HDR for the whole session — `render:cm_auto_hdr` is intentionally
OFF on this machine because every HDR↔SDR toggle full-modesets the output (~1s black on each
workspace switch, i915 limitation):

1. Before (or right after) starting playback: `jarvis-hdr-pin on`
2. When the movie ends / mpv closes: `jarvis-hdr-pin off`
3. `jarvis-hdr-pin status` shows live SDR/HDR per output (reads wp_color_manager_v1).

mpv is already configured (`target-colorspace-hint-mode=source`); the pin costs one ~1s
blank at on and one at off — never during the session. Skip the pin for SDR content and
for `--cast` (the Chromecast handles HDR itself). The user can also toggle with SUPER+ALT+H.

## Safety rule (do not violate)

The JSON **never** contains the stream/debrid URL or token — this is by design (the project
redacts them everywhere). Do not attempt to extract, log, reconstruct, or display any
Torrentio/debrid URL or token. Work only from the descriptive fields in the JSON.
