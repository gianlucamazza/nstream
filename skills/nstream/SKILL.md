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

| Intent | Command |
|--------|---------|
| "metti X sul TV" / "casta X" | `nstream --json --cast "X"` |
| "guarda X" / "riproduci X in locale" | `nstream --json --local "X"` |
| disambiguate by year | `nstream --json --cast --year 1999 "X"` |
| a specific series episode | `nstream --json --cast --season 1 --episode 3 "X"` |
| target a specific TV | `nstream --json --cast --device "Salotto" "X"` |
| with subtitles | add `--subs` (preferred lang) or `--sub-lang ita` |
| force the audio/dub language | add `--audio-lang eng` (e.g. original audio + `--sub-lang ita`) |
| list available audio/subs (no play) | `nstream --json --probe "X"` |
| resume last watched | `nstream --json -c "X"` (or no title for the most recent) |
| something popular / new / top-rated | `nstream --json --cast --browse popolari\|nuovi\|top` |
| stop what's casting | `nstream --json --stop` |
| what's casting now | `nstream --json --status` |
| set the TV volume | `nstream --json --cast --volume 35 "X"` (0–100) |

### Audio & subtitles
- **Subtitles**: `--sub-lang ita` forces Italian subs; `--subs` picks your preferred language.
  The play result reports the active `subtitles` lang.
- **Audio/dub**: `--audio-lang CODE` forces a specific dub (e.g. `eng` for original audio). If no
  stream carries that language the command fails with `error: audio_lang_unavailable` and an
  `available_audio` list — **do not** silently play another language; show the available options
  and ask the user.
- **Discover first**: when the user is unsure ("what languages does X have?"), run `--probe` — it
  returns `available_audio` and `available_subtitles` without playing, so you can present the
  choices, then play with the chosen `--audio-lang`/`--sub-lang`.
- Every play result includes `audio_lang` (the dub played) and `available_audio` (what else was
  on offer), so you can confirm precisely what was started.
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
  remux means a 30-60 GB download, while a *direct* 4K cast streams for free): among Dolby-only
  titles it prefers a 1080p release over a 4K one. It's a preference — a sole 4K Dolby release is
  still cast — and lifts to whatever `cast_remux_max_resolution` is (0 = no cap). Native-AAC titles
  are never capped (they cast direct, no download).
- Toggle/target via config: `cast_remux` (default on), `cast_audio_codec` (default `aac`),
  `cast_remux_max_resolution` (default 1080; 0 = no cap).
- `--stop` also tears down the remux server + temp file. If a remux cast is left running and never
  stopped, the next run garbage-collects the stale temp file.

### Cast lifecycle (nstream is now self-sufficient)
- `nstream --json --status` → `player_state`, `title`, `position`, `duration`, `volume`, `muted`
  of the receiver (use for "what's playing / is it still on?").
- `nstream --json --stop` → stop the cast.
- `nstream --json --cast --volume N "X"` → set the receiver volume (0–100) when starting a cast;
  combine with the `volume`/`muted`/`notice` fields the cast result already reports.
- (For mirroring the desktop or casting an arbitrary file/URL, still use the `skill-cast` skill.)

Notes:
- **Cast is fire-and-return by default** (`--no-follow` implied): the command returns as soon as
  the receiver has the media. Add `--follow` only if the user wants resume/auto-advance tracking
  (it will hold the terminal for the whole runtime).
- **`--follow` streams playback events as JSONL** (one JSON object per line) instead of a single
  final object: `{"action":"cast","event":"started|playing|paused|ended|failed", ...}` with
  `position`/`duration` on the playing/ended lines. Read lines until `event:"ended"` (or
  `event:"failed"`). Without `--follow` you get the usual single summary object.
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

stdout is always a single JSON object. Read `ok`:

- `ok: true` → confirm to the user with `title`, `action` (cast/play), `device`, and a short
  stream summary from `stream` (e.g. "1080p HEVC, audio ita, cached"). `selection` tells you
  whether the title was an `exact` name match or a `first`-result guess — if `first`, mention
  which title was chosen so the user can correct it.
- `ok: false` → handle by `error` code (below).

### Error codes
- `no_result` — nothing matched the title. Offer to retry with a different spelling or add a year.
- `no_streams` — title found but no sources (often "not released yet"); surface `message`.
- `no_playable_stream` — sources exist but none pass the hardware/cast filters; suggest `--local`
  or a lower-quality preference.
- `audio_lang_unavailable` — the requested `--audio-lang` isn't in any stream; show the
  `available_audio` list and ask which dub to use (or drop `--audio-lang`).
- `episode_not_found` — show the `available` seasons/episodes and ask which to play.
- `device_not_found` — no Chromecast resolved, or several TVs and none specified. Run `catt scan`
  to list devices; if more than one, ask the user which (AskUserQuestion) and re-run with
  `--device "<name>"`.
- `usage` — a bad flag combo (e.g. `--sub-menu` with `--json`); fix the command.
- Missing debrid token / config error → nstream exits non-zero and prints to **stderr**; surface
  that line and point the user at `nstream --settings`.

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
