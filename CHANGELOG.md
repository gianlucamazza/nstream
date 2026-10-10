# Changelog

All notable changes to nstream are documented here. Version source:
`src/nstream/__init__.py` (and `packaging/PKGBUILD` `pkgver`).

Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## Unreleased

### Added

- **ADR 0049 Accepted:** Trakt as a user-configured *catalog* addon on Film/Serie
  (**── Trakt ──**). Watch-history / lists / popular hang on `run_browse` →
  `api.catalog` → play. Not an indexer, not a sync of local continue-watching or
  the local watchlist. Secret URL: `trakt_addon` or `NSTREAM_TRAKT_ADDON` (env
  wins), or a Fonti paste. No marketplace, no hardcoded Trakt host, no remux /
  volume change.
- **ADR 0048 Accepted:** genre and skip extras for unlocked addon catalogs on
  Film/Serie. Manifest `genre` options (else Cinemeta tokens) and `skip` paging
  via the existing `run_browse` path. Cinemeta Generi… unchanged. No marketplace
  or Trakt.
- **ADR 0046 Accepted:** catalog board from unlocked addon manifests. Film / Serie TV
  list extra catalogs under **── cataloghi addon ──** (`Addon · name`). No marketplace,
  no new config key. `api.play_id` uses a `tt` already on the row.
- **ADR 0047 Accepted:** translate `tmdb:` / `kitsu:` (and similar catalog ids) to
  IMDb `tt…` via existing meta endpoints before stream discovery. Soft-fail: never
  invent a `tt`; `--json` `error: id_untranslated` when nothing streamable remains.
- **ADR 0045 Proposed:** Philips / catt-only Chromecast RCA. Cast volume CLI is
  0–100% ↔ `SET_VOLUME` 0–1 MASTER (Phase 0: this DMR reports
  `volume_control_type=master`, `volume_step_interval=null`; catt may quantize
  14→13). Cast % is not TV OSD — no `osd_max`. Phase 1: a remote debrid url is
  Range-served from the host LAN when the upstream honours Range
  (`cast_lan_proxy`, default on; JSON `delivery: lan`) so 1080 HEVC stays
  native — not remux-720. No synthesized 206. No-Range / unknown length go to
  the existing remux or live tier. Matroska still follows ADR 0022 in
  `cast_flow` (`-c copy` to MP4, `-tag:v hvc1` for HEVC). Open: OSD photo
  table, board HEVC playback HEAD, JointSpace pairing.
- `--json --status` reports `volume_control_type`, `volume_step_interval`,
  `volume_percent`, `app_id`, `content_type`, `stream_type` (ADR 0045). Never
  `content_id`. `--volume` / TUI copy names Cast % ≠ OSD.

### Fixed

- LAN-proxy `open_upstream` / `probe` pin each hop across **all** `getaddrinfo`
  results (skip blocked, try each public address, `getpeername` per attempt),
  reject IPv4-compatible `::/96` (`::7f00:1`), and ignore env
  `http_proxy`/`https_proxy` (`ProxyHandler({})`) so a proxy cannot bypass the
  pin. Private / loopback / link-local / unspecified / CGNAT / ULA / v4-mapped
  stay rejected.
- **ADR 0051:** a release name that says x265/HEVC can be H.264 (Deadpool field
  report). LAN/remux routing (`hev1` rewrap, `CAST_VIDEO_DECODABLE`) and
  `--json` `stream.codec` use the probed codec when ffprobe already ran;
  otherwise `codec_source: release_name` and the TUI suffix `(claimed)`. No
  extra probe, no remux-720.
- **ADR 0050:** catt **≥0.13.2** remux/file and direct fallback LOADs send
  Cinemeta title + Cinemeta/metahub HTTPS poster (`thumb` → `images[]`) +
  `video/mp4` + `BUFFERED` via `catt.api` / catt's interpreter. Older catt
  (0.13.0/0.13.1) keeps the pre-0050 argv (`-l` would be `cast_failed`). CLI
  fallback still has `-l` + `--stream-type` (no `--thumb`). A library timeout is
  confirmed on the receiver before a second LOAD or remux-server teardown.
  `--json` unchanged.
- catt-only casts with `cast_receiver_app_id` set emit `receiver_app_ignored` instead
  of silently staying on the Default Media Receiver `CC1AD845`.

## [1.43.0] — 2026-10-02

### Added

- `SECURITY.md`: report debrid-token leaks and LAN/remux-server issues via GitHub
  private advisories.
- GitHub Actions Trusted Publisher workflow publishes to PyPI on `v*` tags (OIDC, no
  API token in secrets).

### Changed

- Field notes and tests use RFC 5737 TEST-NET-1 (`192.0.2.10`) and a dummy Cast
  application id (`CA5T0001`) in place of a home LAN address and a personal receiver id.
- Arch `PKGBUILD` source is the GitHub tag tarball over HTTPS (directory
  `nstream-$pkgver`). `sha256sums` stays `SKIP` until the public `v1.43.0` tarball exists.

## [1.42.0] — 2026-10-02

### Added

- **ADR 0044 Accepted:** live `started` is an observed player state
  (PLAYING/PAUSED/BUFFERING). Dual-layer Dolby Vision and a first HLS segment the DMR
  cannot decode skip live; the complete-file remux is the fallback.

### Fixed

- Live fire-and-return no longer reports `ok: true` on a mere LOAD ack, so a DMR that
  fetches the playlist and then ERRORs falls back to the complete-file remux.
- Dual-layer Dolby Vision (enhancement layer) and a first HLS segment without decodable
  audio skip the live path. Profile 8 single-layer stays live. Field 2026-10-02:
  *After Hours* live head refused → `delivery: file` → PLAYING.

## [1.41.0] — 2026-10-02

### Added

- **ADR 0042 Accepted:** a live cast that wants subtitles uses the release's own full text
  track (never a `forced` one) as an HLS WebVTT rendition, activated by castbridge ≥ 0.4.1
  (`textLanguage`). The complete-file path extracts the same track in its pass.
  `subtitles_match: "embedded"`.
- `--sub-shift ±S` (and TUI cast menu ±0.5 s) moves a live cast's subtitles.
- After-start subtitle alignment on live casts: the producer measures speech activity, and
  after 10 min the cues are aligned with the same gates as the complete-file path.
  `--status` reports `subtitles_offset`.
- Live seeks anywhere (producer restart at the target), the TV clock in film time
  (`EXT-X-GAP` filler), bandwidth-aware ranking and a `live_slow` warning.
- Binge: the next episode's live producer is prepared near the end of the current one
  and adopted at the change.
- Interactive (TUI) live casts share the live state: seeks, sub-shift and film-time status.

### Changed

- **ADR 0043 Accepted:** a downloaded subtitle on a live cast is a rendition too (the
  producer's second input), so `--sub-shift` and the after-start alignment only rewrite
  the cues the TV fetches next: the media is never reloaded.
- With castbridge ≥ 0.4.2 a live seek's LOAD rides the running receiver session (no app
  relaunch, no INTERRUPTED playback).

### Fixed

- `--sub-shift` on a live cast no longer leaves the TV IDLE or stuck in BUFFERING (it
  re-LOADed the media).
- A live seek that restarts the producer no longer sends the TV IDLE (ERROR) before the new
  playlist loads: the old generation is kept until the receiver moves on.
- `tests/test_e2e.py` no longer spawns the real castbridge (216 orphaned daemons found).
- `--follow` events report film time and the probed duration on live casts.

## [1.40.0] — 2026-10-01

### Added

- **ADR 0039 Accepted:** a cast whose audio needs conversion (Dolby/DTS, or an `.mkv`
  rewrap) streams a live HLS-TS playlist and starts in seconds instead of after the
  whole-file remux. Stereo AAC; disk bounded to a ~40-min window; the complete file is the
  fallback. `cast_live` config key; JSON `delivery` (`live`/`file`/`direct`/`mirror`/`local`).
- Live seeks: far jumps re-LOAD the playlist at the target (the receiver clamps them);
  a resume starts the producer at the resume point (27 s instead of 223 s for 50 min).

### Changed

- **ADR 0041 Accepted:** with the live tier, a soft `audio_langs` preference starts the
  preferred dub instead of yielding to another one (ADR 0035 stays the fallback when live
  cannot run; its notice now names `--audio-lang <lang>`).

### Fixed

- A live cast's progress reaches history (the playlist reports no duration).
- A live LOAD always uses the Default Media Receiver: the custom receiver refuses HLS.
- A recast of the title the TV is playing (another dub, a retry) resumes where the TV is
  at LOAD time, not where it was when the command began.
- `urlproxy` resumes dropped upstream reads and never prints tracebacks (#5).
- Releases with burned-in CJK subtitles are demoted (CJK-script name on any addon, HC/CHS
  tags) and never picked for an instant direct cast (#6).
- A search whose catalog (Cinemeta) never answered is a `network` error to retry, not
  `no_result` (#4).

## [1.39.0] — 2026-10-01

### Fixed

- **ADR 0036 Accepted:** remux feasibility (disk, size budget) is decided before the
  prepare, never after a multi-GB download; an infeasible conversion fails as
  `remux_infeasible` instead of a mute direct cast. Search, cast ranking and disc-sized
  releases fixed from the 2026-10-01 incident.
- **ADR 0038 Accepted:** a dead source is keyed by what failed, never by a shared
  display name.
- Debrid tokens no longer appear in child-process argv: ffmpeg/ffprobe read through a
  loopback proxy (`urlproxy`), mpv through a private playlist file.
- Subtitles: local audio alignment works (it had failed every time since July: mono
  16 kHz analysis, runtime-based timeout). One cue model (`srt`): CP1252/UTF-16 decoding,
  valid WebVTT (3-digit ms, ASS/`<font>` tags removed, `<`/`&` escaped), cues before 0
  dropped instead of piled at 00:00. A failed download tries the next candidate.
  Downloads are size-capped (gzip bombs) and cached by URL. catt receives the cleaned
  WebVTT. `subs_delivered` follows the receiver's confirmed active tracks; the caption
  track carries a BCP-47 language and a display name.
- Reselects (dub, MP4 twin, castable video, in-cast switch, forced `--audio-lang`) rank
  with the searched title like the initial pick. Accented titles ("Léon") no longer
  demote their own releases.
- Lifecycle: no signal to a reused pid; detached servers and ffmpeg are reclaimed; the
  remux prepare lock is published only once held; the previous cast session is kept
  until a new cast really starts; headless Ctrl-C on a direct cast aborts instead of
  re-casting through catt.
- `--doctor` detects castbridge; series resume follows the next episode with autoplay
  off; headless resolves the TV before any stream work; a zero receiver volume is
  re-read before warning; short language codes ("Chi", "Por") count only in capitals;
  untagged MP4/WebM releases are considered for an instant direct cast; never-started
  addon tasks no longer trip breakers; poster downloads are capped; the P2P privacy
  notice shows once per process.

### Changed

- **ADR 0037 Accepted:** interactivity is an explicit mode; menus and confirmations are
  injected by the frontend, and the domain never opens fzf. One failure table
  (`failures.describe`) for the TUI and `--json`; domain notices reach `--json` as
  `notices` with stable codes (`remux_infeasible`, `subs_unverified`, `volume_zero`, …).
- Performance: cold search 25.6 s → 0.1–1.6 s (bounded half-open probes, search quorum,
  stale-while-revalidate manifests); bounded ffprobe; subtitles are fetched while the
  Tier-2 remux runs.
- Configurable custom receiver app id (ADR 0013).
- The bench-only sparse alignment moved to `_subalign_remote` (not shipped); debrid
  provider keys live in the `providers` leaf; the bottom import tier is test-enforced.

### Proposed

- ADR 0039 (Tier-2 as live HLS-TS; receiver gate 3 passed: 62 min, 0 stalls) and
  ADR 0040 (subtitles off the cast start path).

## [1.38.0] — 2026-09-25

### Added

- Shared local playback service, explicit media-start evidence, and actionable player
  errors. Movies no longer activate the next-episode overlay or quit half a second
  before EOF.
- `--doctor` / `--json --doctor`: read-only local diagnostics. Optional binaries are
  reported apart from required ones.
- Reproducible verification: `scripts/check.sh` (Ruff, ty, pytest, wheel contents,
  disposable install) on Python 3.13 and 3.14, architecture contracts, subprocess
  playback acceptance, benchmarks, and an mpv smoke that does not touch the desktop
  or the TV.
- **ADR 0034 Accepted:** bounded addon gathering, deadline-aware retries, bounded JSON
  decoding, validated stream fields, collector-owned breaker updates, serialized
  best-effort state writes, atomic runtime snapshots, and a private recovery copy
  before a malformed JSON file is replaced. Existing state files stay compatible.

### Changed

- **ADR 0035 Accepted:** a soft `audio_langs` preference no longer forces a full-file
  remux when a verified direct cast (MP4/WebM, decodable first track) exists at the
  same quality. Headless starts that direct cast, keeps primary-language safety
  subtitles, and explains the choice in `notice`. A TUI asks once. `--audio-lang`
  still waits for the preferred dub. Remux progress is printed even when stderr is
  not a terminal.
- Playback no longer changes the host firewall. Capability URLs are redacted in logs
  and machine-readable output.
- The development-only subtitle benchmark is excluded from the wheel with `exclude`,
  not `force-exclude`.

## [1.37.1] — 2026-08-10

### Fixed

- **ADR 0033 Accepted:** a title with nothing playable no longer returns to the menu in
  silence. `stream_select.prepare_stream` reserves `None` for the user backing out (ESC) and
  raises `NoPlayableStream(reason)` on exhaustion; the reason reaches the fzf header (TUI) and
  the `no_playable_stream` message (headless). Typical case: every source is a pure torrent and
  the P2P privacy gate (ADR 0032) is closed, or the debrid left no direct link.

### Added

- `--explain`: `SORGENTI: n/m con link diretto · k torrent` line, plus `counts.direct_links`,
  `counts.torrents` and `unresolvable_reason` in the JSON output — the same diagnosis before
  attempting playback.
- `engine.p2p_block_reason`: the privacy gate's predicate without its side effects, so
  explainers can state the refusal without triggering it.

## [1.37.0] — 2026-08-09

### Added

- **ADR 0027 Accepted:** per-addon circuit breaker (`state/breaker.py`) on gather paths;
  Open sources skipped without network; `--forget-breakers`; listed in `--explain`.
- TUI cast session menu: pause / resume / relative seek / refresh / volume / stop; cast row
  also on Film/Serie sections (`cast_control`).
- Settings: cast/P2P/subs knobs, hybrid `playback_backend: auto`, `default_quality`,
  diagnostics for required bins (`mpv`, `fzf`) and optional cast/P2P deps.
- Config load tightens world-readable `config.json` to 0600 (debrid token hygiene).
- Multi-addon gather progress on TTY (completion order, not submit order).

### Fixed

- Alt-C reuses full cast decision tree (`run_cast`); quality miss surfaces available res;
  `default_quality` on headless + TUI paths.
- Headless-only flags without `--json` are usage errors (no silent TUI no-op).

### Documentation

- Full documentation restructure: audience hub (`docs/README.md`), user guides
  (`docs/user/`), official headless contract (`docs/headless.md`), config reference tables,
  cast/TUI/troubleshooting pages, CONTRIBUTING, roadmap/non-goals, CHANGELOG.
- Aligned `config.example.json` with `Config` defaults (`playback_backend: local`, all keys
  including `cast_mirror_over_remux_gb` / `sub_align*`).
- ADR 0026/0027 normalized to English MADR template; 0027 status **Accepted**.
- Config/example drift test (`tests/test_config_docs.py`).

## [1.36.1] — 2026-08-08

- P2P privacy gate enforced on every swarm-join path (ADR 0032).

## [1.36.0] and earlier — summary of major features

Historical releases were not logged in this file. Major capabilities present by 1.36.x:

- Terminal-first Stremio-like client (Cinemeta, fzf, mpv), zero runtime Python deps.
- Multi-source streams (Torrentio + user manifests; ADR 0024).
- Playback backends: local TorrServer P2P, debrid, auto hybrid, native TorBox/Premiumize.
- Hardware-aware ranking, cast-aware ranking, dead-source denylist, duration vetting.
- Cast stack: castbridge + catt fallback, Tier-2 remux, realtime mirror, discovery cache.
- Headless `--json` API for agents/scripts; series binge overlay; resume / continue-watching.
- Native subtitle alignment (ADR 0020); structured stream metadata precedence (ADR 0026).

See [docs/adr/](docs/adr/README.md) for the decision timeline.
