# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

nstream is a native, terminal-first Stremio alternative: no Electron, no embedded browser,
no Node server. Pure Python 3.13+ stdlib (zero runtime dependencies) orchestrating external
CLIs (`fzf`, `mpv`, `ffprobe`, `catt`) over Stremio addon HTTP APIs. Entry point is
`nstream.cli:_entry` (`src/nstream/cli.py`).

Pipeline: search/browse (Cinemeta) → pick title/episode (fzf) → optional subtitle pick
(OpenSubtitles v3) → fetch streams (Torrentio + debrid) → hardware-aware rank/filter →
play (mpv) or cast (Chromecast via catt) → track progress (resume / continue-watching).

## Commands

Tooling is `uv`-based (no venv activation needed).

| Task | Command |
|------|---------|
| Run tests | `uv run pytest` |
| Single test | `uv run pytest tests/test_cli.py::test_display_title_movie` |
| Lint | `uvx ruff check . && uvx ruff format --check .` |
| Type check | `uvx ty check` |
| Dev run | `uv run nstream "the matrix"` |
| Install (dev) | `./install.sh` (uses `uv tool install`) |

Ruff: line-length 100, rules `E,F,I,UP,B,SIM`. Pytest: `testpaths=["tests"]`, `-q`.
Each `src/nstream/<mod>.py` has a matching `tests/test_<mod>.py`.

## Architecture

Modules in `src/nstream/`:
- `cli.py` — orchestrator: argparse, fzf pickers, mpv/cast launch, resume, series auto-advance.
- `api.py` — HTTP addon dispatch with retry/backoff, gzip, concurrent aggregation
  (`ThreadPoolExecutor`, ≤8 workers), 600s in-process metadata cache (streams/subs NOT cached).
- `addons.py` — Stremio addon protocol client + manifest registry/cache.
- `quality.py` — stream parsing + hardware-aware ranking (GPU caps via `vainfo`, cached).
- `config.py` — XDG config load/save (atomic temp+replace), typed schema.
- `state.py` — watch history (resume / continue-watching).
- `tracks.py` — ffprobe audio/subtitle track probing (graceful degradation if absent).
- `settings.py` — fzf-based settings menu (RD token, addons, hwdec…).
- `log.py` — rotating file log + crash capture + secret redaction.
- `nstream.lua` — mpv overlay (resume toast + next-episode card).

### Debrid: provider-agnostic
The debrid token is embedded in the Torrentio base URL — `cfg.torrentio_base` is
`sort=qualitysize|{provider}={token}`, requested as `torrentio.strem.fun/{base}/manifest.json`.
8 providers supported (realdebrid, alldebrid, premiumize, torbox, debridlink, easydebrid,
offcloud, putio). Cached-marker detection (`[RD+]`, `[AD+]`, `[TB+]`, …) and log redaction
are all handled generically (single regex), never per-provider. **When touching debrid logic,
keep it provider-agnostic** — don't special-case RealDebrid.

### Casting (Chromecast via catt)
- Discovery: `settings.py:scan_devices()` runs `catt scan` → `(name, ip)` pairs.
- Resolution: `cli.py:_resolve_device()` honors `cfg.cast_device` only if present on the
  current LAN; otherwise re-discovers. Stored/resolved **by IP** (robust to mDNS flakiness).
- Playback: `catt cast <url> [-d ip] [-t start] [-s subs]`, polled via `catt info -j` every 15s.
- In-cast audio switch ('a'): re-casts the same title with a different dub via already-fetched streams.
- Cast ranking ignores the language filter (show all dubs) but avoids Cast-incompatible
  audio (TrueHD/DTS-HD/DTS → would be silent).

### mpv ↔ nstream signalling
Per-play temp dir under `$XDG_RUNTIME_DIR/nstream-*/` holds the mpv IPC socket, a signal
file, subtitle files, and the next-episode label.
- Resume: passed as `--start=<seconds>` (nstream is the sole source of truth;
  `--no-resume-playback` blocks mpv's own watch-later).
- Position tracking: daemon thread reads `time-pos`/`duration` over `--input-ipc-server`.
- Overlay (`nstream.lua`) signals back by writing to the signal file: `"next"` (next episode)
  or `"cast"` (in-player Alt-C move-to-TV → nstream re-resolves a device and casts from current position).
- Track choices passed as `--aid`/`--sid`/`--sub-file`; `--alang`/`--slang` injected unless user-set.

### Hardware-aware decode
`quality.py` caches `vainfo` output to `$XDG_CACHE_HOME/nstream/vainfo.json`.
`cli.py:_hwdec_defaults()` upgrades the ambiguous `auto`-family hwdec to the GPU's real
method (e.g. vaapi) so mpv doesn't probe unsupported paths. A concrete method in `mpv.conf`
is always respected.

### Logging & secrets
`log.py` writes `$XDG_STATE_HOME/nstream/nstream.log` (rotating, 512 KB × 3). `_entry()`
calls `setup_logging()` before `main()` so even argparse tracebacks are captured (the foot
launcher scrolls stderr away). A `RedactFilter` scrubs `{provider}=<token>` and
`/resolve/{provider}/<token>/` on every record. **Never log Torrentio URLs in clear** — use
the `what=` addon-name string in errors; `addons.py` caches manifests keyed by URL hash.
`--debug` / `NSTREAM_DEBUG=1` adds a stderr handler at DEBUG level.

## Conventions & gotchas
- fzf pickers use module-level `object()` sentinels (`_PLAY`, `_ALL`, …) so `None` can still
  mean "user pressed ESC" without ambiguity.
- All disk I/O (log, history, cache) is best-effort try/except — never block playback.
- Config writes are atomic (temp + replace); addon fetch failures fall back to stale cache.
- Comments/docs in English; concise, no over-engineering.

## Config & paths
- Config: `$XDG_CONFIG_HOME/nstream/config.json` (chmod 600; template `config.example.json`).
- History: `$XDG_STATE_HOME/nstream/history.json`.
- Caches: `$XDG_CACHE_HOME/nstream/` (`manifests.json`, `vainfo.json`).
- Runtime deps: `mpv`, `fzf` (required); `ffmpeg`/`ffprobe`, `catt`, `vainfo`, `foot` (optional).
