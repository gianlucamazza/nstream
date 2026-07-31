# CLAUDE.md

Guidance for agents working in this repository.

## Project

nstream is a native, terminal-first Stremio alternative: no Electron, no embedded browser,
no Node server. Pure Python 3.13+ stdlib (zero runtime dependencies) orchestrating external
CLIs (`fzf`, `mpv`, `ffprobe`, `catt`, `chafa`) over Stremio addon HTTP APIs. Entry point is
`nstream.cli:_entry` (`src/nstream/cli.py`).

Pipeline: search/browse (Cinemeta) → pick title/episode (fzf) → optional subtitle pick
(OpenSubtitles v3) → fetch streams (Torrentio + debrid) → hardware-aware rank/filter →
play (mpv) or cast (Chromecast via castbridge/catt) → track progress (resume / continue-watching).

## Commands

Tooling is `uv`-based (no venv activation needed).

| Task          | Command                                                                  |
| ------------- | ------------------------------------------------------------------------ |
| Run tests     | `uv run python -m pytest`                                                |
| Single test   | `uv run python -m pytest tests/test_labels.py::test_display_title_movie` |
| Lint          | `uvx ruff check . && uvx ruff format --check .`                          |
| Type check    | `uvx ty check`                                                           |
| Dev run       | `uv run nstream "the matrix"`                                            |
| Install (dev) | `./install.sh` (uses `uv tool install`)                                  |

Use `python -m pytest` (not bare `uv run pytest`): if a system-wide `nstream` is installed,
bare `pytest` resolves to the system interpreter and imports the **installed** package instead
of `src/`. `python -m` runs the project venv with the editable `src/` checkout.

Ruff: line-length 100, rules `E,F,I,UP,B,SIM`. Pytest: `testpaths=["tests"]`, `-q`.
Each `src/nstream/<mod>.py` has a matching `tests/test_<mod>.py`.

## Architecture (source of truth)

**Module map and domain clusters:** [`docs/architecture.md`](docs/architecture.md)  
**Stream ranking:** [`docs/selection.md`](docs/selection.md)  
**Decisions:** [`docs/adr/`](docs/adr/README.md)

Do not duplicate the module-by-module list here — update `docs/architecture.md` when adding
or moving modules.

### Import-graph discipline (non-negotiable)

- `util` / `ui` / `languages` / `labels` sit at the top
- `cli` orchestrates at the bottom
- Everything below `cli` **never** imports `cli` (including `headless`, `cast_flow`,
  `stream_select`, `cast_vet`, `player`, `caster`, …)
- Cast policy lives in `cast_vet`; cast decision tree in `cast_flow`; backends in
  `caster` / `remux` / `mirror`. Selection facade: `stream_select.prepare_stream`.
  Availability probes/denylist: `availability`. Domain TypedDicts: `types` (not `config`).
  Headless play body: `headless_play`; argparse: `cli_args`. State package: `state/`

### Hard constraints when editing

- **Debrid provider-agnostic:** never special-case RealDebrid; tokens/markers/redaction are generic
- **Never log stream URLs or debrid tokens in clear** — use `what=` labels; `log.RedactFormatter`
- **Cast audio language** is decided at selection (`cast_vet.vet_cast_audio`), not on the DMR
- **Dead sources (ADR 0025):** only `gone` probes are persisted; incomplete reads are not proof
- **Disk I/O best-effort** try/except — never block playback
- fzf sentinels are module-level `object()` values (`_PLAY`, `_ALL`, …) so `None` means ESC
- Comments/docs in English; concise, no over-engineering
- **`ui.Caps`** = terminal caps; **`quality.HwCaps`** = GPU/vainfo — different types

### Stream sources (ADR 0024)

Torrentio (`torrentio_enabled`) + `cfg.addons` manifests. `api.streams` stamps `addon`, fuses
debrid url with torrent infoHash by filename, collapses duplicates (cached > url > infoHash).

### Secrets & paths

- Config: `$XDG_CONFIG_HOME/nstream/config.json` (chmod 600; template `config.example.json`)
- History: `$XDG_STATE_HOME/nstream/history.json`
- Dead sources: `$XDG_STATE_HOME/nstream/dead-sources.json` (`--forget-dead`)
- Caches: `$XDG_CACHE_HOME/nstream/` (`manifests.json`, `hwcaps.json`, `devices.json`, `meta/`, `posters/`)
- Log: `$XDG_STATE_HOME/nstream/nstream.log` (rotating); `--debug` / `NSTREAM_DEBUG=1` → stderr DEBUG
- Runtime deps: `mpv`, `fzf` (required); `ffmpeg`/`ffprobe`, `catt`, `vainfo`, `chafa`, `foot` (optional)

## Packaging hygiene

`packaging/` tracked files: `PKGBUILD`, `build-local.sh`, `nstream.install`, `.SRCINFO` only.
Build artifacts (`pkg/`, `src/`, `*.pkg.tar.zst`, `*.sig`) are gitignored; `build-local.sh`
cleans them. Do not commit wheels or makepkg trees.
