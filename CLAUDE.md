# CLAUDE.md

Guidance for agents working in this repository.

## Project

nstream is a native, terminal-first Stremio alternative: no Electron, no embedded browser,
no Node server. Pure Python 3.13+ stdlib (zero runtime dependencies) orchestrating external
CLIs (`fzf`, `mpv`, `ffprobe`, `catt`, `chafa`, optional TorrServer / castbridge) over Stremio
addon HTTP APIs. Entry point is `nstream.cli:_entry` (`src/nstream/cli.py`).

Pipeline: search/browse (Cinemeta + catalogs) → pick title/episode (fzf or `--json`) → optional
subtitles (OpenSubtitles v3 + alignment) → fetch streams (Torrentio and/or `cfg.addons`) →
hardware- or cast-aware rank/filter → play (mpv) or cast (castbridge/catt/remux/mirror) →
track progress (resume / continue-watching).

## Commands

Tooling is `uv`-based (no venv activation needed).

| Task | Command |
| ---- | ------- |
| Run tests | `uv run python -m pytest` |
| Single test | `uv run python -m pytest tests/test_labels.py::test_display_title_movie` |
| Lint | `uvx ruff check . && uvx ruff format --check .` |
| Type check | `uvx ty check` |
| Dev run | `uv run nstream "the matrix"` |
| Install (dev) | `./install.sh` (uses `uv tool install`) |

Use `python -m pytest` (not bare `uv run pytest`): if a system-wide `nstream` is installed,
bare `pytest` resolves to the system interpreter and imports the **installed** package instead
of `src/`. `python -m` runs the project venv with the editable `src/` checkout.

Ruff: line-length 100, rules `E,F,I,UP,B,SIM`. Pytest: `testpaths=["tests"]`, `-q`.
Each `src/nstream/<mod>.py` has a matching `tests/test_<mod>.py` (or package coverage).

## Documentation map (do not re-duplicate)

| Topic | Source of truth |
| ----- | --------------- |
| Module map / domains | [`docs/architecture.md`](docs/architecture.md) |
| Stream ranking / cast vet | [`docs/selection.md`](docs/selection.md) |
| Headless `--json` contract | [`docs/headless.md`](docs/headless.md) |
| Config keys | [`docs/user/config.md`](docs/user/config.md) |
| Decisions (why) | [`docs/adr/`](docs/adr/README.md) |
| Hub / audience routing | [`docs/README.md`](docs/README.md) |
| Agent playbook (intent→cmd) | [`skills/nstream/SKILL.md`](skills/nstream/SKILL.md) |
| Contributor process | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

Update architecture when adding/moving modules — **not** the README module list (there is none).

### Import-graph discipline (non-negotiable)

- Top: `util` / `ui` / `languages` / `types` / `sources` / `net` / `log` (see architecture)
- `cli` orchestrates at the bottom
- Everything below `cli` **never** imports `cli` (including `headless`, `cast_flow`,
  `stream_select`, `cast_vet`, `player`, `caster`, …)
- Cast policy lives in `cast_vet`; cast decision tree in `cast_flow`; backends in
  `caster` / `remux` / `mirror`; TUI cast lifecycle in `cast_control`. Selection facade:
  `stream_select.prepare_stream`. Availability probes/denylist: `availability`. Domain
  TypedDicts: `types` (not `config`). Headless play body: `headless_play`; argparse:
  `cli_args`. State package: `state/` (history, cast_session, dead, breaker)

### Hard constraints when editing

- **Debrid provider-agnostic:** never special-case RealDebrid; tokens/markers/redaction are generic
- **Never log stream URLs or debrid tokens in clear** — use `what=` labels; `log.RedactFormatter`
- **Cast audio language** is decided at selection (`cast_vet.vet_cast_audio`), not on the DMR
- **Dead sources (ADR 0025):** only `gone` probes are persisted; incomplete reads are not proof
- **P2P privacy gate (ADR 0032):** at every swarm join, not one call site
- **Disk I/O best-effort** try/except — never block playback
- fzf sentinels are module-level `object()` values (`_PLAY`, `_ALL`, …) so `None` means ESC
- Comments/docs in English; concise, no over-engineering
- **`ui.Caps`** = terminal caps; **`quality.HwCaps`** = GPU/vainfo — different types
- New architectural trade-off → ADR ([template](docs/adr/0000-template.md)); cite symbols not lines

### Stream sources (ADR 0024)

Torrentio (`torrentio_enabled`) + `cfg.addons` manifests. `api.streams` stamps `addon`, fuses
debrid url with torrent infoHash by filename, collapses duplicates (cached > url > infoHash).

### Secrets & paths

- Config: `$XDG_CONFIG_HOME/nstream/config.json` (chmod 600; template `config.example.json`)
- History: `$XDG_STATE_HOME/nstream/history.json`
- Library: `$XDG_STATE_HOME/nstream/library.json` (recent + watchlist metadata only)
- Dead sources: `$XDG_STATE_HOME/nstream/dead-sources.json` (`--forget-dead`)
- Addon breakers: `$XDG_STATE_HOME/nstream/addon-breakers.json` (`--forget-breakers`, ADR 0027)
- Caches: `$XDG_CACHE_HOME/nstream/` (`manifests.json`, `hwcaps.json`, `devices.json`, `meta/`, `posters/`, `torrents/`)
- Log: `$XDG_STATE_HOME/nstream/nstream.log` (rotating); `--debug` / `NSTREAM_DEBUG=1` → stderr DEBUG
- Runtime deps: `mpv`, `fzf` (required); `ffmpeg`/`ffprobe`, `catt`, TorrServer, `vainfo`, `chafa`, `foot`, castbridge/mirror (optional)

## Packaging hygiene

`packaging/` tracked files: `PKGBUILD`, `build-local.sh`, `nstream.install`, `.SRCINFO` only.
Build artifacts (`pkg/`, `src/`, `*.pkg.tar.zst`, `*.sig`) are gitignored; `build-local.sh`
cleans them. Do not commit wheels or makepkg trees.
