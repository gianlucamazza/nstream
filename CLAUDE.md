# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

## Architecture

Modules in `src/nstream/`:

- `cli.py` — orchestrator: argparse, TUI flow (home + typed Film/Serie sections), resume,
  `--explain`/`__preview` commands, `--movies`/`--series` type filters. Delegates stream selection
  to `stream_select`, subtitles to `subs`, the series flow to `series`, the whole `--json` mode to
  `headless`, label formatting to `labels`; sits at the bottom of the import graph.
- `headless.py` — the `--json` subsystem: `run()` is the single seam `cli._dispatch` calls when
  `--json` is set — non-interactive title/episode resolution (`_select_meta`), play/cast
  (`_auto_play`, via `cast_flow.run_cast` + the volume guard), `--probe`/`--stop`/`--status`/`-c`,
  JSONL `--follow` events, and the NetworkError → JSON-error guard. Exactly one JSON object per
  invocation; no stream url/token ever reaches stdout. Also home to `typ_filter` (shared with
  `cli`). Same tier as `cast_flow`: imports `api`/`bridge`/`cast_flow`/`caster`/`mirror`/`player`/
  `quality`/`remux`/`series`/`state`/`stream_select`/`subs`/`labels`/`config`/`ui`; never `cli`,
  never `picker`/fzf.
- `series.py` — the series-only flow (ADR 0009): episode picker, binge auto-advance loop,
  per-episode resume (`play`/`binge`/`resume`/`entry_video`). The player entry point is injected
  as a callable (`PlayVideo` Protocol), so it never imports `cli`; imports
  `api`/`state`/`labels`/`picker`/`config`/`ui` (+ `caster.CastMeta`).
- `stream_select.py` — stream selection + resolution + auto-play vetting guards. `prepare_stream()`
  is the single entry the orchestrator calls (pre-commit cached verification → pick+resolve →
  cached-miss fallback → primary-language audio guard), returning a `VettedStream`. The pre-commit
  step (ADR 0014, `_verify_cached_availability`, auto + non-local only) probes the top-N `[XX+]`
  cached candidates concurrently and demotes any dead one (strips its marker via `quality._CACHED_RE`,
  the inverse of `_mark_native_cached`) so the auto-pick re-ranks around what actually responds —
  keeping it off a stale link and out of an accidental Tier-2 remux. Probes are memoized
  (`_probe_url`) and shared with the last-resort `_ensure_playable`. Also `cast_languages`/`cast_resolver` for the in-cast
  switch. Imports `api`/`debrid`/`engine`/`quality`/`remux`/`tracks`/`languages`/`picker`/
  `labels`/`config`/`log`/`ui`; never `cli`.
- `cast_flow.py` — shared cast decision tree: `run_cast()` is the single body behind the
  interactive cast (`cli._play_on_cast`) and the headless `--json --cast` branch
  (`headless._auto_play`) — vet the audio plan (`vet_cast_audio`) → absent-dub safety subtitles →
  mirror gate → Tier-2 remux → direct cast, returning a `CastOutcome`
  (action/stream/reencoded/notice/audio/subs) for the caller's JSON accounting. Device resolution
  and the headless volume guard stay in the callers (`cli`/`headless`). Same tier as
  `stream_select`: imports `caster`/`engine`/`mirror`/`remux`/`quality`/`stream_select`/`subs`/
  `config`/`log`/`ui`; never `cli`.
- `subs.py` — subtitle acquisition: `pick_subtitles` (OpenSubtitles fetch/rank/download), `auto_subs`
  (no-menu paths + safety-subtitle net). Leaf below `cli`; imports `api`/`picker`/`config`.
- `labels.py` — presentation helpers (`meta_label`/`stream_label`/`episode_label`/`history_label`/
  `display_title`/`track_label`/`audio_summary`/`sub_summary`). Reads active caps on demand via
  `ui.active_caps()`. Top tier: imports only `ui`/`quality`/`tracks`/`config`.
- `api.py` — HTTP addon dispatch (`ThreadPoolExecutor` ≤8; retry/backoff/gzip via `net`); 600s
  in-process metadata cache **plus** an on-disk metadata cache (`meta_cached_disk`,
  `$XDG_CACHE_HOME/nstream/meta/`); streams/subs NOT cached.
- `addons.py` — Stremio addon protocol client + manifest registry/cache.
- `net.py` — retrying HTTP-JSON GET (gzip, exponential backoff honouring `Retry-After`) +
  `url_playable` reachability probe, shared by `api`/`addons`; split out of `api` to break the
  former `addons ↔ api` cycle. Leaf (imports only `log`/`util` + stdlib); error messages carry a
  `what=` label, never the URL.
- `quality.py` — stream parsing + hardware-aware ranking (GPU caps via `vainfo`, cached).
- `player.py` — local mpv playback: launch, position tracking over the IPC socket, and the
  `*_defaults` helpers (hwdec/quiet/lang/stream-cache) that decide what to inject without
  overriding the user. The stream-cache defaults (bigger demuxer readahead +
  pause-to-rebuffer) mitigate A/V desync on network streams; mirror inherits them too.
  **Imports nothing from `cli`** (no cycle).
- `cast_delivery.py` — shared castbridge delivery driver (ADR 0011): `drive_bridge` owns the
  bridge event loop and its policy (failed-before-started → catt fallback, started announce,
  pos/dur tracking, CAST_DONE finish heuristic, Ctrl-C: receiver stop + per-delivery hook).
  `caster` and both `remux` bridge branches are thin adapters over it. Leaf (imports only
  `bridge`/`log` + stdlib); home of the canonical `EventCb` and `CAST_DONE`.
- `caster.py` — Chromecast playback: `resolve_device()` (via `discovery`: verified cache → short
  bounded wait on the background scan, Ctrl-C skips to local), `cast()` (castbridge LOAD when
  available, else `catt`), status poll, in-cast audio switch (catt-only). Imports `bridge`,
  `discovery`, `ui` (glyphs) and the picker from `picker`, not `cli`.
- `discovery.py` — background Chromecast discovery (ADR 0010): the `catt scan` primitive
  (`scan_sync`, moved out of `settings`), a daemon-thread singleton started at TUI startup
  (`start_background`/`get_devices`), a 24h disk cache (`$XDG_CACHE_HOME/nstream/devices.json`)
  and a ~1s TCP probe to the cast control port (`verify`). Leaf (imports only `log`/`util` +
  stdlib); never prints — user-facing messaging stays in callers.
- `bridge.py` — IPC client for the **castbridge** daemon (AF_UNIX newline-JSON, stdlib `socket` only):
  metadata-rich LOAD + normalized event stream (started/playing/paused/ended/failed/disconnected).
  The session lives in the daemon, so a fire-and-return load survives our exit (ADR 0007/0008).
  Leaf below `cli` (imports only `log` + stdlib).
- `serve.py` — Tier-2 cast delivery: minimal **Range-capable HTTP server** (206/`Content-Range`)
  serving the complete remux file to the DMR — in-process for the `follow` path, detached
  (`python -m nstream.serve`) for headless fire-and-return. Also serves an optional side-loaded
  **WebVTT caption track** (second token path, CORS headers the receiver requires) so castbridge
  can LOAD subtitles; `spawn_detached`/`kill_detached` (shared detached-server helper) and a
  single-slot `register_sub_server`/`reap_sub_server` for the Tier-1 direct-cast VTT server. Also
  `ensure_firewall()` (ufw rule for
  the receiver's inbound fetch). Leaf: stdlib + `log`.
- `remux.py` — Tier-2 cast for titles whose audio the Chromecast can't decode (AC-3/E-AC-3/DTS/
  TrueHD → silent). Detects them (ffprobe), remuxes to a complete temp MP4 (video `-c copy`, audio
  → AAC) on disk, then serves it: nstream's own Range server (`serve.py`) on the castbridge path,
  a **detached catt** as fallback (a complete, Range-served file is the only delivery this DMR
  accepts — see `docs/adr/0005`/`0007`). `needs_remux`/`remux_for_cast`/`remux_to_file`/`cast_file`/
  `stop`. Leaf below `cli` (imports `bridge`/`caster`/`serve`/`config`/`log`/`ui` + stdlib), like
  `engine`/`player`.
- `mirror.py` — realtime cast backend (`--mirror` / `cast_mode: "mirror"`, ADR 0006): mpv decodes
  the stream locally on a Hyprland **headless output** + PipeWire null sink, and the openscreen
  Cast Streaming sender (`$CAST_MIRROR_BIN`) mirrors that window to the TV (~120ms, 1080p SDR —
  instant start, no download/remux). Leaf below `cli` (imports `player`/`config`/`log`/`ui`/`util` + stdlib).
- `engine.py` — local P2P backend: drives an external **TorrServer** (find/spawn, add torrent by
  infoHash, wait for the read-ahead buffer) and returns a plain `http://…/stream?…` url — the same
  contract as a debrid url. Leaf below `cli` (imports only `config`/`log`/`util`/`ui` + stdlib), like
  `caster`/`player`. Best-effort: raises `EngineUnavailable` instead of crashing the picker.
  `magnet_from_stream()` is shared with `debrid`; `detach_spawned()` lets a spawned server outlive
  a headless fire-and-return cast (the Chromecast keeps streaming from it).
- `debrid.py` — native debrid backend: talks **directly** to a provider's API (TorBox/Premiumize)
  to batch-check the cache and resolve an infoHash to an `http://…` url — same contract as `engine`.
  Used only by the `native` playback backend; RealDebrid is intentionally absent (no cache endpoint
  since 2024 — see `docs/adr/0002`). Leaf below `cli` (imports `config`/`engine`/`log` + stdlib);
  best-effort, raises `DebridUnavailable` → caller degrades to P2P. `get_resolver`/`DebridResolver`.
- `picker.py` — shared fzf pickers (TUI flow + cast menus); imports only `util`+`ui`. `fzf`/`fzf_key`.
- `preview.py` — body of the hidden `nstream __preview` subcommand: poster thumbnail (via `chafa`)
  plus metadata card in the fzf preview pane. Best-effort, **never prints stream URLs**.
- `ui.py` — TUI design system: capability detection, palette/glyph set/fzf theme, progress bars,
  layout breakpoints. Near the top of the import graph; must never import `api`/`picker`/`cli`/`quality`/`caster`.
- `explain.py` — diagnostic renderer for `--explain`: reconstructs the auto-pick decision with the
  same primitives the player uses (`languages`/`player`/`quality`/`tracks`/`ui`). Read-only.
- `languages.py` — **single source of truth** for languages (release tokens, flags, display names);
  formerly three hand-synced maps. Leaf module (imports nothing from nstream).
- `config.py` — XDG config load/save (atomic temp+replace), typed schema (incl. `posters`, `nerd_font`);
  also home to the `PlayOpts` per-invocation value object (config-shaped, imported everywhere).
- `state.py` — watch history (resume / continue-watching), inter-process locked writes, and
  the fire-and-return **cast session** (`RunState("watch")`, keyed by the **resolved IP**):
  a headless cast records what's on the TV (`note_started`/`remember_cast`) so
  `--stop`/`--status` can merge the receiver's real position back into history
  (`update_from_receiver`) — the headless surface writes history on every path (`--follow`,
  `--local`, fire-and-return). Staleness guards: session TTL (6h) + opportunistic receiver
  title match; zero-progress "started" entries are pruned after 7 days and a series binge
  retires its started siblings; every new cast clears the previous session
  (`clear_cast_session` in `cast_flow.run_cast` / Alt-C).
- `tracks.py` — ffprobe audio/subtitle track probing (graceful degradation if absent).
- `settings.py` — fzf-based settings menu (debrid token, addons, hwdec, cast device…).
- `log.py` — rotating file log + crash capture + secret redaction.
- `util.py` — low-level helpers: atomic write, best-effort JSON load, subprocess launch. Top of the
  import graph, stdlib-only.
- `nstream.lua` — mpv overlay (resume toast + next-episode card).

**Import-graph discipline:** `util`/`ui`/`languages`/`labels` sit at the top (little or no internal
imports), `cli` orchestrates at the bottom; everything below `cli` —
`headless`/`player`/`caster`/`cast_delivery`/`picker`/`stream_select`/`cast_flow`/`subs`/`series`/`labels`/
`engine`/`debrid`/`remux`/`mirror`/`serve`/`bridge`/`net`/`discovery`/`preview`/`explain` — never imports
`cli`. This is the recurring constraint that explains where logic lives — preserve it when
moving code.

### Debrid: provider-agnostic

The debrid token is embedded in the Torrentio base URL — `cfg.torrentio_base` is
`sort=qualitysize|{provider}={token}`, requested as `torrentio.strem.fun/{base}/manifest.json`.
8 providers supported (realdebrid, alldebrid, premiumize, torbox, debridlink, easydebrid,
offcloud, putio). Cached-marker detection (`[RD+]`, `[AD+]`, `[TB+]`, …) and log redaction
are all handled generically (single regex), never per-provider. **When touching debrid logic,
keep it provider-agnostic** — don't special-case RealDebrid. A proposed native-resolver adapter
layer that would narrow (not abandon) this principle is recorded in `docs/adr/` (ADR 0001–0004).

### Casting (Chromecast)

- **Sender backend (ADR 0007):** casting prefers the native **castbridge** daemon (built in the
  `cast` repo's openscreen fork) over catt, because catt can't send media metadata. `bridge.py`
  speaks castbridge's AF_UNIX IPC (stdlib only): a metadata-rich LOAD (title/poster/season/episode
  → TV now-playing card + JARVIS HUD widget) plus a real `media-status`/`session` event stream that
  drives `--follow` JSONL and resume/auto-advance. `caster.cast`/`remux.cast_file` use castbridge
  when its binary is present (`CASTBRIDGE_BIN` or the openscreen-build path) and **fall back to catt**
  (no metadata) otherwise, when it can't start, or for the interactive 'a' audio switch (catt-only).
  Tier-2 remux is served by nstream's own stdlib **Range server** (`serve.py`), not catt, on the
  castbridge path. **Subtitles ride both senders**: the SRT is converted to WebVTT and side-loaded
  as an active caption track — served next to the media on the Tier-2 path, or by a small
  single-slot standalone server (`serve.register_sub_server`) on the Tier-1 direct path; catt still
  uses `-s`. `caster.cast`/`remux.cast_file` take `sub_lang` (labels the track); the daemon
  `media-load` gained `subtitleUrl`/`subtitleLang`/`subtitleName`. `--follow` emits one JSON line
  per event (started/playing/paused/ended/failed/
  `disconnected` — daemon socket EOF mid-cast: the receiver may still be playing, it is **not**
  `ended`, so Tier-2 keeps its Range server alive and leaves the temp file to `--stop`/GC).
- **Tier-2 firewall:** the receiver fetches the remux file _inbound_ from the host, so `serve.py`
  binds catt's cast range (45000-47000) and `ensure_firewall` auto-adds the matching ufw rule
  (`sudo -n ufw allow from <lan>/24 to any port 45000:47000 proto tcp`, idempotent, identical to
  skill-cast's `cast-screen fw-setup` so they share one rule). Best-effort: a no-op without
  ufw/passwordless-sudo, with a `firewall_hint` printed on a Tier-2 startup failure. Tier-1 and the
  daemon channel are outbound — no rule, nstream stays unprivileged for ordinary casts.
- Discovery (ADR 0010): `discovery.py` runs `catt scan` → `(name, ip)` pairs in a **background
  daemon thread** kicked off at TUI startup (`cli._dispatch`), refreshing a 24h disk cache
  (`devices.json`). `resolve_device` uses a cache-verified device instantly (~1s TCP probe to
  port 8009), waits at most 6s (25s for an explicit picker) on the pending scan — Ctrl-C skips
  to local — and rescues from the cache when an mDNS scan is flaky. Cast never freezes the TUI;
  headless callers block deterministically. The settings device picker scans synchronously
  (explicit user action) and doubles as a manual cache refresh.
- Resolution: `caster.py:resolve_device()` honors `cfg.cast_device` only if present on the
  current LAN; otherwise re-discovers. Stored/resolved **by IP** (robust to mDNS flakiness).
- Playback: `caster.py:cast()` runs `catt cast <url> [-d ip] [-t start] [-s subs]`, polled via
  `catt info -j` every 15s.
- In-cast audio switch ('a'): re-casts the same title with a different dub via already-fetched streams.
- **Audio language is enforced at selection time** (`stream_select.vet_cast_audio`), because the
  DMR plays a file's _first_ audio track and **can't switch embedded audio tracks** — per Google
  Cast docs only _text_ tracks are selectable on the Default Media Receiver; audio selection needs
  a custom (registered, non-libre) receiver. So `vet_cast_audio` ffprobes the chosen dub and returns
  a `CastAudioPlan`: **direct** when the first track is already primary-language + decodable;
  **remux** (keeping only the primary-language audio track, mapped with ffmpeg `0:a:N`) when the
  language is present but not the playable first track; **absent** → reselect another dub, else cast with
  primary-language safety subtitles. The remux _is_ the cast's `--aid` (the only libre way to pick
  an embedded audio track). `--audio-lang` overrides the target; default is `cfg.primary`.
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
    dual-audio release casts the right dub) and scales the AAC bitrate to the channel count. The
    decision always comes from a real ffprobe of the chosen url — never the release name — memoized
    per url for the process (`tracks.probe_tracks`), so the audio guard, the cast vetting and the
    pre-remux metadata read share one network probe. Against a runaway 4K download,
    `cast_remux_max_size_gb` (default 20) both **demotes** oversized likely-remux
    releases in ranking (`quality.remux_within_size` — size is the real cost an unlabelled 4K REMUX
    hides from the resolution cap) and asks for **confirmation** on an interactive tty before fetching;
    a free-disk pre-check always runs (~size×1.1 when the release size is parsed, a `_MIN_FREE_GB`
    floor when it's unknown).
  - Last resort for what even the DMR can't play: the in-tree realtime H.264 1080p mirror
    (`mirror.py`, `--mirror` / `cast_mode: "mirror"` — ADR 0006). Beyond the forced flag, the
    mirror is **auto-preferred over a pathological remux** (ADR 0015): when a remux would fetch a
    4K Dolby-only release ≥ `cast_mirror_over_remux_gb` GB (default 10; a size-unknown 4K counts),
    `cast_flow` mirrors instead — instant start, no 30-60 GB download, at the cost of 1080p SDR
    (surfaced in the outcome `notice`). Below the threshold the remux still wins (native video/HDR).
    `0` disables the auto-switch.
  - Dolby/DTS are no longer _excluded_ from cast (they were "silent") — only ranked below native AAC.

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
launcher scrolls stderr away). A `RedactFormatter` scrubs `{provider}=<token>` and
`/resolve/{provider}/<token>/` on every fully formatted record — appended exception
tracebacks included, which a record-level filter would miss. **Never log Torrentio URLs in clear** — use
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
- Caches: `$XDG_CACHE_HOME/nstream/` (`manifests.json`, `vainfo.json`, `devices.json` Chromecast
  discovery, `meta/` disk metadata, `posters/` thumbnails).
- Runtime deps: `mpv`, `fzf` (required); `ffmpeg`/`ffprobe`, `catt`, `vainfo`, `chafa`, `foot` (optional).
