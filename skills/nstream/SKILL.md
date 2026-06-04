---
name: nstream
description: >
  Play or cast movies and TV series headlessly via the nstream CLI. Use when the user wants to
  watch / put on / stream a specific film or series — locally on this laptop (mpv) or on the TV
  (Chromecast). Always non-interactive: drives `nstream --json` (no fzf, no TTY) and parses the
  JSON result. For raw Chromecast control (mirror the desktop, stop, volume) use the `skill-cast`
  skill instead — nstream only plays media it resolves itself. Trigger keywords: metti sul TV,
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
| resume last watched | `nstream --json -c "X"` (or no title for the most recent) |

Notes:
- **Cast is fire-and-return by default** (`--no-follow` implied): the command returns as soon as
  the receiver has the media. Add `--follow` only if the user wants resume/auto-advance tracking
  (it will hold the terminal for the whole runtime).
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
- `episode_not_found` — show the `available` seasons/episodes and ask which to play.
- `device_not_found` — no Chromecast resolved, or several TVs and none specified. Run `catt scan`
  to list devices; if more than one, ask the user which (AskUserQuestion) and re-run with
  `--device "<name>"`.
- `usage` — a bad flag combo (e.g. `--sub-menu` with `--json`); fix the command.
- Missing debrid token / config error → nstream exits non-zero and prints to **stderr**; surface
  that line and point the user at `nstream --settings`.

## Safety rule (do not violate)

The JSON **never** contains the stream/debrid URL or token — this is by design (the project
redacts them everywhere). Do not attempt to extract, log, reconstruct, or display any
Torrentio/debrid URL or token. Work only from the descriptive fields in the JSON.
