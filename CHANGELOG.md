# Changelog

All notable changes to nstream are documented here. Version source:
`src/nstream/__init__.py` (and `packaging/PKGBUILD` `pkgver`).

Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

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
