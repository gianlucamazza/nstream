# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

nstream is a native, terminal-first Stremio alternative: no Electron, no embedded browser,
no Node server. Pure Python 3.13+ stdlib (zero runtime dependencies) orchestrating external
CLIs (`fzf`, `mpv`, `ffprobe`, `catt`, `chafa`) over Stremio addon HTTP APIs. Entry point is
`nstream.cli:_entry` (`src/nstream/cli.py`).

Pipeline: search/browse (Cinemeta) → pick title/episode (fzf) → optional subtitle pick
(OpenSubtitles v3) → fetch streams (Torrentio + debrid) → hardware-aware rank/filter →
play (mpv) or cast (Chromecast via catt) → track progress (resume / continue-watching).

## Commands

Tooling is `uv`-based (no venv activation needed).

| Task | Command |
|------|---------|
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
Each `src/nstream/<mod>.py` has a matching `tests/test_<mod>.py`.

## Architecture

Modules in `src/nstream/`:
- `cli.py` — orchestrator: argparse, TUI flow, resume, series auto-advance, `--explain`/`__preview`
  commands. Delegates stream selection to `stream_select`, subtitles to `subs`, label formatting to
  `labels`; sits at the bottom of the import graph.
- `stream_select.py` — stream selection + resolution + auto-play vetting guards. `prepare_stream()`
  is the single entry the orchestrator calls (pick+resolve → cached-miss fallback → primary-language
  audio guard), returning a `VettedStream`. Also `cast_languages`/`cast_resolver` for the in-cast
  switch. Imports `api`/`quality`/`engine`/`tracks`/`languages`/`picker`/`labels`; never `cli`.
- `subs.py` — subtitle acquisition: `pick_subtitles` (OpenSubtitles fetch/rank/download), `auto_subs`
  (no-menu paths + safety-subtitle net). Leaf below `cli`; imports `api`/`picker`/`config`.
- `labels.py` — presentation helpers (`meta_label`/`stream_label`/`episode_label`/`history_label`/
  `display_title`/`track_label`/`audio_summary`/`sub_summary`). Reads active caps on demand via
  `ui.active_caps()`. Top tier: imports only `ui`/`quality`/`tracks`/`config`.
- `api.py` — HTTP addon dispatch (retry/backoff, gzip, `ThreadPoolExecutor` ≤8); 600s in-process
  metadata cache **plus** an on-disk metadata cache (`meta_cached_disk`, `$XDG_CACHE_HOME/nstream/meta/`);
  streams/subs NOT cached.
- `addons.py` — Stremio addon protocol client + manifest registry/cache.
- `quality.py` — stream parsing + hardware-aware ranking (GPU caps via `vainfo`, cached).
- `player.py` — local mpv playback: launch, position tracking over the IPC socket, and the
  `*_defaults` helpers (hwdec/quiet/lang) that decide what to inject without overriding the user.
  **Imports nothing from `cli`** (no cycle).
- `caster.py` — Chromecast playback via `catt`: `resolve_device()`, `cast()`, status poll, in-cast
  audio switch. Imports the picker from `picker`, not `cli`.
- `remux.py` — Tier-2 cast for titles whose audio the Chromecast can't decode (AC-3/E-AC-3/DTS/
  TrueHD → silent). Detects them (ffprobe), remuxes to a complete temp MP4 (video `-c copy`, audio
  → AAC) on disk, then lets a **detached catt** serve it (the only delivery this DMR accepts —
  see `docs/adr/0005`). `needs_remux`/`prepare_for_cast`/`cast_file`/`stop`. Leaf below `cli`
  (imports `caster`/`config`/`log` + stdlib), like `engine`/`player`.
- `engine.py` — local P2P backend: drives an external **TorrServer** (find/spawn, add torrent by
  infoHash, wait for the read-ahead buffer) and returns a plain `http://…/stream?…` url — the same
  contract as a debrid url. Leaf below `cli` (imports only `config`/`log`/`util` + stdlib), like
  `caster`/`player`. Best-effort: raises `EngineUnavailable` instead of crashing the picker.
  `magnet_from_stream()` is shared with `debrid`.
- `debrid.py` — native debrid backend: talks **directly** to a provider's API (TorBox/Premiumize)
  to batch-check the cache and resolve an infoHash to an `http://…` url — same contract as `engine`.
  Used only by the `native` playback backend; RealDebrid is intentionally absent (no cache endpoint
  since 2024 — see `docs/adr/0002`). Leaf below `cli` (imports `config`/`engine`/`log` + stdlib);
  best-effort, raises `DebridUnavailable` → caller degrades to P2P. `get_resolver`/`DebridResolver`.
- `picker.py` — shared fzf pickers (TUI flow + cast menus); imports only `util`+`ui`. `fzf`/`fzf_key`.
- `preview.py` — body of the hidden `nstream __preview` subcommand: poster thumbnail (via `chafa`)
  + metadata card in the fzf preview pane. Best-effort, **never prints stream URLs**.
- `ui.py` — TUI design system: capability detection, palette/glyph set/fzf theme, progress bars,
  layout breakpoints. Near the top of the import graph; must never import `api`/`picker`/`cli`/`quality`/`caster`.
- `explain.py` — diagnostic renderer for `--explain`: reconstructs the auto-pick decision with the
  same primitives the player uses. Read-only.
- `languages.py` — **single source of truth** for languages (release tokens, flags, display names);
  formerly three hand-synced maps. Leaf module (imports nothing from nstream).
- `config.py` — XDG config load/save (atomic temp+replace), typed schema (incl. `posters`, `nerd_font`);
  also home to the `PlayOpts` per-invocation value object (config-shaped, imported everywhere).
- `state.py` — watch history (resume / continue-watching).
- `tracks.py` — ffprobe audio/subtitle track probing (graceful degradation if absent).
- `settings.py` — fzf-based settings menu (debrid token, addons, hwdec…), `scan_devices()`.
- `log.py` — rotating file log + crash capture + secret redaction.
- `util.py` — low-level helpers: atomic write, best-effort JSON load, subprocess launch. Top of the
  import graph, stdlib-only.
- `nstream.lua` — mpv overlay (resume toast + next-episode card).

**Import-graph discipline:** `util`/`ui`/`languages`/`labels` sit at the top (little or no internal
imports), `cli` orchestrates at the bottom; everything below `cli` —
`player`/`caster`/`picker`/`stream_select`/`subs`/`labels`/`engine`/`preview`/`explain` — never
imports `cli`. This is the recurring constraint that explains where logic lives — preserve it when
moving code.

### Debrid: provider-agnostic
The debrid token is embedded in the Torrentio base URL — `cfg.torrentio_base` is
`sort=qualitysize|{provider}={token}`, requested as `torrentio.strem.fun/{base}/manifest.json`.
8 providers supported (realdebrid, alldebrid, premiumize, torbox, debridlink, easydebrid,
offcloud, putio). Cached-marker detection (`[RD+]`, `[AD+]`, `[TB+]`, …) and log redaction
are all handled generically (single regex), never per-provider. **When touching debrid logic,
keep it provider-agnostic** — don't special-case RealDebrid. A proposed native-resolver adapter
layer that would narrow (not abandon) this principle is recorded in `docs/adr/` (ADR 0001–0004).

### Casting (Chromecast via catt)
- Discovery: `settings.py:scan_devices()` runs `catt scan` → `(name, ip)` pairs.
- Resolution: `caster.py:resolve_device()` honors `cfg.cast_device` only if present on the
  current LAN; otherwise re-discovers. Stored/resolved **by IP** (robust to mDNS flakiness).
- Playback: `caster.py:cast()` runs `catt cast <url> [-d ip] [-t start] [-s subs]`, polled via
  `catt info -j` every 15s.
- In-cast audio switch ('a'): re-casts the same title with a different dub via already-fetched streams.
- Cast ranking ignores the language filter (show all dubs) and models the **Default Media
  Receiver**: it plays HEVC/4K/HDR natively but **doesn't decode Dolby** (AC-3/E-AC-3/DTS/TrueHD →
  silent). Two tiers:
  - **Tier 1 — direct**: audio already AAC/Opus → `caster.cast(url)` (DMR streams it, even 4K, no
    host download). Selection **prefers AAC** so this is the common, instant path.
  - **Tier 2 — remux** (`remux.py`, when `cfg.cast_remux`): Dolby/DTS audio → host remuxes to a
    complete temp MP4 (video `-c copy` → native HEVC/4K/HDR kept, audio → AAC) and a detached catt
    serves it. The DMR only plays a complete, Range-served file (streaming-while-transcoding fails on
    it — `docs/adr/0005`), so the whole file is fetched first (a prepare wait, shown as an ffmpeg
    `-progress` percentage). A **remux-only resolution cap** (`cast_remux_max_resolution`, default
    1080p) avoids downloading a full 4K just to cast; direct 4K casts are uncapped (free, no download).
    The remux picks the audio track matching the user's language priority (not blindly `0:a:0`, so a
    dual-audio release casts the right dub) and scales the AAC bitrate to the channel count. A single
    pre-cast ffprobe drives this (a positively-AAC release name skips it → instant Tier-1). Against a
    runaway 4K download, `cast_remux_max_size_gb` (default 20) both **demotes** oversized likely-remux
    releases in ranking (`quality.remux_within_size` — size is the real cost an unlabelled 4K REMUX
    hides from the resolution cap) and asks for **confirmation** on an interactive tty before fetching;
    a free-disk pre-check always runs.
  - Last resort for what even the DMR can't play: the H.264 1080p mirror (`skill-cast`/openscreen).
  - Dolby/DTS are no longer *excluded* from cast (they were "silent") — only ranked below native AAC.

### mpv ↔ nstream signalling
Per-play temp dir under `$XDG_RUNTIME_DIR/nstream-*/` holds the mpv IPC socket, a signal
file, subtitle files, and the next-episode label.
- Resume: passed as `--start=<seconds>` (nstream is the sole source of truth;
  `--no-resume-playback` blocks mpv's own watch-later).
- Position tracking: `player.py` daemon thread reads `time-pos`/`duration` over `--input-ipc-server`.
- Overlay (`nstream.lua`) signals back by writing to the signal file: `"next"` (next episode)
  or `"cast"` (in-player Alt-C move-to-TV → nstream re-resolves a device and casts from current position).
- Track choices passed as `--aid`/`--sid`/`--sub-file`; `--alang`/`--slang` injected unless user-set.

### Hardware-aware decode
`quality.py` caches `vainfo` output to `$XDG_CACHE_HOME/nstream/vainfo.json`.
`player.py:_hwdec_defaults()` upgrades the ambiguous `auto`-family hwdec to the GPU's real
method (e.g. vaapi, via `quality.preferred_hwdec(quality.detect_caps())`) so mpv doesn't probe
unsupported paths. A concrete method in `mpv.conf` is always respected.

### TUI preview & `--explain`
- `--explain <query>` (`cli.py:run_explain` → `explain.py`) reconstructs and prints WHY a stream/audio
  was auto-picked, using the same primitives as playback (`quality.rank_streams`/`score_components`,
  `player._lang_defaults`, `tracks.probe_tracks`). Read-only — it never plays.
- fzf preview pane: fzf spawns `nstream __preview` per focused row (`preview.py`), rendering a poster
  thumbnail via `chafa` + a metadata card. Gated by `cfg.posters` and the terminal's image-protocol
  support; any failure degrades to a minimal card. Only meta is rendered — no stream URL can leak.

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
- Caches: `$XDG_CACHE_HOME/nstream/` (`manifests.json`, `vainfo.json`, `meta/` disk metadata, `posters/` thumbnails).
- Runtime deps: `mpv`, `fzf` (required); `ffmpeg`/`ffprobe`, `catt`, `vainfo`, `chafa`, `foot` (optional).
