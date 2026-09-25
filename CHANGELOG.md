# Changelog

All notable changes to nstream are documented here. Version source:
`src/nstream/__init__.py` (and `packaging/PKGBUILD` `pkgver`).

Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## Unreleased

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
