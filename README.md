# nstream

Native, terminal-first Stremio alternative. No Electron, no embedded browser, no Node
server — just Python stdlib driving `fzf` and `mpv` over the Stremio addon HTTP APIs.

Replicates the core Stremio flow:

1. **Search** a title via **Cinemeta** (official Stremio metadata addon), or
   **browse** a catalog (Popular / New / Top-rated).
2. Pick the title (and, for series, the episode) with **fzf**.
3. Optionally pick **subtitles** via the **OpenSubtitles v3** addon.
4. Fetch streams from **Torrentio** using your existing Real-Debrid config.
5. Play the chosen stream in **mpv** (Real-Debrid resolves it to a direct link).

Watch progress is tracked over mpv's IPC socket, so **resume** and a
**continue-watching** menu work across sessions. For series, a Netflix-style
**next-episode overlay** appears near the end and auto-advances the binge.

## Why

The Stremio Flatpak beta (new Rust shell) crashes on Wayland/Hyprland (ozone-X11
`Missing X server`) and the Qt build is heavy. nstream reuses the same Torrentio +
Real-Debrid pipeline with a fraction of the footprint, and integrates with the native
mpv setup.

## Requirements

Runtime: `python3` (>=3.13, **zero third-party deps**), `mpv`, `fzf`, plus `foot` for the
desktop launcher. Install/dev: `uv`. All native.

## Install

| Method | Command | Notes |
|--------|---------|-------|
| Arch repo | `sudo pacman -Syu nstream` | from the `[gianluca]` personal repo |
| Arch (local) | `cd packaging && makepkg -si` | builds from the tagged source tarball |
| PyPI | `pipx install nstream` | or `pip install --user nstream` |
| From source (dev) | `./install.sh` | `uv tool install` → `~/.local/bin/nstream` |

The Arch package installs `nstream` + `nstream-fuzzel` to `/usr/bin`, the desktop entry, and
`config.example.json` under `/usr/share/nstream/`. It does **not** touch `$HOME`: after install,
create your config (see below). `./install.sh` instead bootstraps `~/.config/nstream/config.json`
from the example automatically.

## Develop

```sh
uvx ruff check . && uvx ruff format --check .   # lint + format
uvx ty check                                    # type check
uv run pytest                                    # unit tests
uv run nstream "the matrix"                      # run from source
```

Runtime stays stdlib-only; `ruff`/`ty`/`pytest` are dev-group tools.

## Config — `~/.config/nstream/config.json`

```json
{
  "torrentio_base": "sort=qualitysize|realdebrid=YOUR_RD_API_TOKEN",
  "cinemeta": "https://v3-cinemeta.strem.io",
  "mpv_args": []
}
```

- `torrentio_base`: the Torrentio config string (everything between the host and
  `/manifest.json` in your Torrentio addon URL). Get your Real-Debrid API token at
  <https://real-debrid.com/apitoken>.
- `opensubtitles`: base URL of the OpenSubtitles v3 addon (default shown).
- `subtitle_langs`: preferred subtitle languages, in order. Used to sort/auto-pick subtitles
  and passed to mpv as `--slang` (non-overriding).
- `audio_langs`: preferred audio languages, in order (default `["ita","eng"]`). Passed to mpv as
  `--alang` so your language is auto-selected when the file has multiple audio tracks — injected
  only if you haven't set `alang` yourself. `--subs-with-matching-audio=no` is also added so
  subtitles aren't forced on when the audio is already in your language.
- `addons`: extra Stremio addon manifest URLs (e.g. another stream or subtitle provider). Streams
  and subtitles are aggregated across the built-in providers plus these. Manage them from the
  settings menu.
- `history_enabled`: keep a watch history for resume / continue-watching (`true` by default).
- `hwdec`: mpv hardware decoding mode (default `auto-safe`). nstream passes `--hwdec=<value>`
  **only if** you haven't already set `hwdec` in `~/.config/mpv/mpv.conf` or in `mpv_args` —
  your own mpv config always wins. Set `""` to disable the injection entirely.
- `autoplay`: show the in-video next-episode overlay for series and auto-advance (`true` by
  default). The overlay is drawn by a bundled mpv Lua script loaded via `--script` — it does
  **not** touch your `mpv.conf`. During a binge, subtitles (`--subs`) and stream selection are
  picked automatically per episode.
- `autoplay_lead`: seconds before the end of an episode at which the overlay appears (default `15`).
- `mpv_quiet`: hide mpv's track list and decoder/driver warnings, keeping the progress line and
  real errors (`true` by default). Injected as `--msg-level` only if you haven't set `msg-level`
  in `mpv.conf`/`mpv_args`. Toggle it from the settings menu.
- `hw_filter`: hardware-aware stream ranking (`true` by default). Torrentio sorts by size, so the
  first result is usually an 8K "AI upscale" or a 60-100GB Dolby Vision REMUX. nstream detects what
  the GPU can decode (via `vainfo`, cached) and auto-picks the best stream it can actually play;
  unsupported streams (8K, Dolby Vision P5, codecs with no HW decode) are excluded from auto-pick
  and shown at the bottom of the stream menu marked `⚠` (still selectable).
- `max_resolution`: cap for the filter (default `2160`; `0` = no cap).
- `allow_software`: keep streams whose codec the GPU can't decode in hardware (default `false`).
- `allow_dv5`: keep Dolby Vision Profile 5 streams (default `false`; they look wrong without DV).
- `mpv_args`: extra flags passed to mpv (e.g. `["--sub-auto=fuzzy"]`).

The file holds your RD token, so it is created `chmod 600` and git-ignored.
Watch history lives separately in `~/.local/state/nstream/history.json` (no secrets).

## Security & limitations

- **Real-Debrid token.** It lives only in `config.json` (`chmod 600`). The Torrentio
  playback URL embeds the token by design (same as Stremio), so it is passed to `mpv` on
  its command line — visible to your own user via `/proc`, but nstream never prints it, never
  writes it to a log, and never persists it elsewhere. nstream also runs mpv with
  `--no-resume-playback` (nstream owns resume), and mpv's default
  `--write-filename-in-watch-later-config=no` keeps the URL out of `watch_later` files.
- **Autoplay overlay.** If you set `--end` in `mpv_args`, the overlay won't appear (mpv still
  reports the full media duration). With `keep-open=yes` the auto-advance still fires, via the
  `time-pos`/`end-file` path.

## Usage

```sh
nstream "the matrix"        # search, pick with fzf, play
nstream                     # continue-watching menu (if any), else prompts for a query
nstream --play "dune"       # auto-pick the top stream, skip the stream menu
nstream --subs "dune"       # auto-pick subtitles in your preferred language
nstream --sub-menu "dune"   # pick subtitles by hand (fzf)
nstream --sub-lang eng ...  # force the auto-picked subtitle language
nstream --browse            # browse the Popular catalog (movies + series)
nstream --browse nuovi      # browse New; also: popolari, top
nstream -c                  # continue watching from history (resumes + keeps bingeing)
nstream --settings          # open the settings menu (also: ⚙ entry in the startup menu)
nstream --no-autoplay ...   # don't show the next-episode overlay
nstream --no-history ...    # don't record this session
```

From Hyprland: launch **nstream** from your app launcher → it opens a **home menu** in foot
(continue-watching · 🔍 search · 🔥 popular · 🆕 new · ⭐ top · ⚙ settings). All UI lives in the
TUI; the launcher only opens it. ESC steps back one level; after a title plays (or has no
sources) you return to the list rather than the app quitting.

## Settings

`nstream --settings` (or the **⚙ Impostazioni** entry in the startup menu) opens a native fzf
menu to edit languages, autoplay, hardware decoding, history, the Real-Debrid token (entered
masked, never printed), and **Stremio addons** — add/remove extra manifest URLs to aggregate more
stream/subtitle/catalog providers alongside the built-in Cinemeta/Torrentio/OpenSubtitles. On
first run, if no config exists, nstream prompts for the Real-Debrid token and writes one.

## Files

| Path | Role |
|------|------|
| `src/nstream/cli.py` | argparse entry point, fzf/mpv orchestration, subtitles, resume |
| `src/nstream/api.py` | addon resource dispatch (search/catalog/streams/subtitles) with retry/backoff |
| `src/nstream/addons.py` | Stremio addon-protocol client (manifests, dispatch, cache) |
| `src/nstream/quality.py` | hardware-aware stream parsing/ranking (vainfo caps, filter) |
| `src/nstream/settings.py` | native fzf settings menu (config + addons) |
| `src/nstream/config.py` | config load/save (XDG, atomic 0600) + payload types |
| `src/nstream/state.py` | watch-history persistence (resume / continue-watching) |
| `src/nstream/nstream.lua` | mpv overlay for the next-episode countdown (loaded via `--script`) |
| `nstream-fuzzel` | thin launcher → opens the TUI home menu in foot |
| `nstream.desktop` | app launcher entry |
| `pyproject.toml` | metadata, entry point, ruff/ty config |
| `config.example.json` | config template (no token) |
| `install.sh` | uv tool install + desktop + config bootstrap |

## Possible extensions

- `--cast` via `catt` to send the stream to a Chromecast.
- Trakt sync; alternative debrid providers (AllDebrid, Premiumize).
