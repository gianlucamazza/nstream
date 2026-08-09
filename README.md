# nstream

Native, terminal-first Stremio alternative. No Electron, no embedded browser, no Node
server — Python 3.13+ **stdlib only**, driving `fzf`, `mpv`, and optional cast/P2P CLIs over
Stremio addon HTTP APIs.

**Docs hub:** [docs/README.md](docs/README.md)

## What it does

1. **Search / browse** titles (Cinemeta and other catalog addons).
2. **Pick** title and episode in a fzf TUI (or headless `--json` for scripts/agents).
3. **Optional subtitles** (OpenSubtitles v3 + alignment; ADR 0020).
4. **Fetch streams** from Torrentio and/or extra manifests (Comet, MediaFusion, … — ADR 0024).
5. **Rank & filter** for your GPU or Chromecast (hardware-aware + cast-aware).
6. **Play** in mpv and/or **cast** (castbridge / catt / remux / mirror).
7. **Resume** and continue-watching; series binge with next-episode overlay.

Playback backends: **local P2P** (TorrServer, default), **debrid** (eight Torrentio providers),
**auto** hybrid, or **native** provider API (TorBox / Premiumize). See
[docs/user/config.md](docs/user/config.md).

## Why

The Stremio Flatpak beta (Rust shell) crashes on Wayland/Hyprland (ozone-X11 `Missing X server`);
the Qt build is heavy. nstream reuses the Stremio addon ecosystem with a fraction of the
footprint and native mpv / Cast integration.

## Requirements

| | Packages |
| - | -------- |
| **Required** | `python3` ≥ 3.13 (zero third-party deps), `mpv`, `fzf` |
| **Strongly recommended** | `ffmpeg`/`ffprobe` (tracks, remux), `foot` (desktop launcher) |
| **Optional** | `catt` (cast discovery/fallback), `chafa` (posters), `vainfo`/`libva-utils` (HW ranking), TorrServer (local P2P) |
| **Optional native cast** | castbridge (`$CASTBRIDGE_BIN`), mirror sender (`$CAST_MIRROR_BIN`) — openscreen-fork builds; fallback without them |

Install/dev tooling: `uv`.

## Install

| Method | Command | Notes |
| ------ | ------- | ----- |
| Arch repo | `sudo pacman -Syu nstream` | `[gianluca]` personal repo |
| Arch (local) | `cd packaging && makepkg -si` | from tagged source tarball |
| PyPI | `pipx install nstream` | or `pip install --user nstream` |
| From source (dev) | `./install.sh` | `uv tool install` → `~/.local/bin/nstream` |

The Arch package installs binaries + desktop entry + `config.example.json` under
`/usr/share/nstream/`; it does **not** write `$HOME`. `./install.sh` bootstraps
`~/.config/nstream/config.json` from the example when missing.

## Quickstart

```sh
# 1) Config (defaults = local P2P; no paid service)
#    For debrid: set playback_backend to "debrid" and put the key in torrentio_base —
#    see docs/user/config.md
nstream --settings

# 2) Play
nstream "the matrix"          # TUI search → Enter plays best stream
nstream --cast "dune"         # Chromecast
nstream --json --cast "dune"  # headless JSON for scripts/agents
```

Full config tables: [docs/user/config.md](docs/user/config.md).  
TUI keys: [docs/user/tui.md](docs/user/tui.md).  
Cast guide: [docs/user/cast.md](docs/user/cast.md).  
Headless contract: [docs/headless.md](docs/headless.md).

## Develop

```sh
uvx ruff check . && uvx ruff format --check .
uvx ty check
uv run python -m pytest          # not bare pytest — see CONTRIBUTING.md
uv run nstream "the matrix"
```

Contributor guide: [CONTRIBUTING.md](CONTRIBUTING.md).  
Module map: [docs/architecture.md](docs/architecture.md).  
Stream ranking: [docs/selection.md](docs/selection.md).  
Decisions: [docs/adr/](docs/adr/README.md).

## Usage

```sh
# Interactive
nstream "the matrix"              # search; Enter = auto-play; Tab = manual
nstream                           # continue-watching or prompt
nstream --cast "dune"             # cast (castbridge, else catt)
nstream --mirror "dune"           # realtime mirror (1080p SDR)
nstream --local "dune"            # force mpv when prefer_cast is on
nstream --play "dune"             # force auto-pick
nstream --subs / --sub-menu / --sub-lang eng …
nstream --sub-offset -2.5 …       # constant subtitle shift (seconds)
nstream --sub-fps 25:23.976 …     # framerate drift correction
nstream --browse [popolari|nuovi|top]
nstream --movies | --series …
nstream -c / --continue           # resume from history
nstream --no-history              # do not read/write watch history this run
nstream --no-autoplay             # skip next-episode overlay
nstream --settings
nstream --explain "dune"          # ranking dump, no play
nstream --quality 1080 "matrix"   # hard resolution filter (aliases: 4k, 720, auto)
nstream --forget-dead             # clear proven-gone denylist (ADR 0025)
nstream --forget-breakers         # clear per-addon circuit breakers (ADR 0027)
nstream --debrid-test INFOHASH    # native debrid diagnostics
nstream --debug / --version

# Headless (--json): one JSON object on stdout — see docs/headless.md
nstream --json --cast --device TV "dune"
nstream --json --cast --follow …          # JSONL until end
nstream --json --local "dune"
nstream --json --probe "dune"             # languages / resolutions (no play)
nstream --json --year 1999 --movies "X"
nstream --json --season 1 --episode 3 "Show"
nstream --json --audio-lang eng --quality 4k --cast "…"
nstream --json --stop | --status | --pause | --resume
nstream --json --seek 1250 | --volume 35
nstream --json --no-mirror --cast "…"
nstream --json -c "Show"                  # continue / next episode
```

Launcher: app menu → home (continue · search · watchlist · Film · Serie · settings).  
Cheatsheet: [docs/user/tui.md](docs/user/tui.md).

## Logging & security

- Log: `$XDG_STATE_HOME/nstream/nstream.log` (rotating); `--debug` / `NSTREAM_DEBUG=1` → stderr.
- Dead sources: `$XDG_STATE_HOME/nstream/dead-sources.json` (ADR 0025).
- Debrid token only in `config.json` (`chmod 600`). Redacting formatter scrubs tokens from
  **every** log line including tracebacks. nstream never prints stream URLs.
- mpv gets `--no-resume-playback` (nstream owns resume); URLs stay out of `watch_later`.

Diagnostics detail: [docs/user/troubleshooting.md](docs/user/troubleshooting.md).

## Documentation map

| Doc | Role |
| --- | ---- |
| [docs/README.md](docs/README.md) | Audience hub |
| [docs/user/config.md](docs/user/config.md) | Every config key |
| [docs/user/cast.md](docs/user/cast.md) | Chromecast delivery |
| [docs/user/tui.md](docs/user/tui.md) | Menus & keys |
| [docs/user/troubleshooting.md](docs/user/troubleshooting.md) | Failures & recovery |
| [docs/headless.md](docs/headless.md) | `--json` contract |
| [docs/selection.md](docs/selection.md) | How streams are chosen |
| [docs/architecture.md](docs/architecture.md) | Where code lives |
| [docs/adr/](docs/adr/README.md) | Why (ADRs) |
| [docs/roadmap.md](docs/roadmap.md) | Non-goals & Proposed ADRs |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev workflow |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |

## License

MIT — see [LICENSE](LICENSE).
