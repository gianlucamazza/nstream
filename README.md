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
**continue-watching** menu work across sessions.

## Why

The Stremio Flatpak beta (new Rust shell) crashes on Wayland/Hyprland (ozone-X11
`Missing X server`) and the Qt build is heavy. nstream reuses the same Torrentio +
Real-Debrid pipeline with a fraction of the footprint, and integrates with the native
mpv setup.

## Requirements

Runtime: `python3` (>=3.13, **zero third-party deps**), `mpv`, `fzf`, plus `fuzzel` +
`foot` for the GUI launcher. Install/dev: `uv`. All native.

## Install

```sh
./install.sh
```

Installs the CLI with `uv tool install` (entry point → `~/.local/bin/nstream`), drops the
`nstream-fuzzel` helper and desktop entry, and creates `~/.config/nstream/config.json`
from the example if absent.

## Develop

```sh
uvx ruff check . && uvx ruff format --check .   # lint + format
uvx ty check                                    # type check
uv run nstream "the matrix"                      # run from source
```

Runtime stays stdlib-only; `ruff`/`ty` are dev-group tools.

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
- `subtitle_langs`: preferred subtitle languages, in order (used to sort the picker).
- `history_enabled`: keep a watch history for resume / continue-watching (`true` by default).
- `mpv_args`: extra flags passed to mpv (e.g. `["--sub-auto=fuzzy"]`).

The file holds your RD token, so it is created `chmod 600` and git-ignored.
Watch history lives separately in `~/.local/state/nstream/history.json` (no secrets).

## Usage

```sh
nstream "the matrix"      # search, pick with fzf, play
nstream                   # continue-watching menu (if any), else prompts for a query
nstream --play "dune"     # auto-pick the top stream, skip the stream menu
nstream --subs "dune"     # also pick subtitles (OpenSubtitles) before playing
nstream --browse          # browse the Popular catalog (movies + series)
nstream --browse nuovi    # browse New; also: popolari, top
nstream -c                # continue watching from history
nstream --no-history ...  # don't record this session
```

From Hyprland: launch **nstream** in fuzzel → type a title → pick in the fzf TUI.

## Files

| Path | Role |
|------|------|
| `src/nstream/cli.py` | argparse entry point, fzf/mpv orchestration, subtitles, resume |
| `src/nstream/api.py` | Cinemeta/Torrentio/OpenSubtitles HTTP with retry/backoff |
| `src/nstream/config.py` | config + state path (XDG) + payload types |
| `src/nstream/state.py` | watch-history persistence (resume / continue-watching) |
| `nstream-fuzzel` | fuzzel prompt → opens the TUI in foot |
| `nstream.desktop` | app launcher entry |
| `pyproject.toml` | metadata, entry point, ruff/ty config |
| `config.example.json` | config template (no token) |
| `install.sh` | uv tool install + desktop + config bootstrap |

## Possible extensions

- `--cast` via `catt` to send the stream to a Chromecast.
- Trakt sync; alternative debrid providers (AllDebrid, Premiumize).
