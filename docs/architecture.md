# Architecture — module map

Single source of truth for **where code lives**. Decision *why* → [`docs/adr/`](adr/README.md).
Stream ranking *how* → [`docs/selection.md`](selection.md). Agent commands & constraints →
[`CLAUDE.md`](../CLAUDE.md).

**Layout:** mostly flat `src/nstream/*.py` (stdlib-only runtime). `state/` is a small package
because it owns four independent stores. Domains below are logical clusters.

```
src/nstream/
├── orchestrazione   cli · cli_args · headless · headless_play · series · settings
├── selezione        stream_select · availability · quality · tracks · sources · debrid · engine
├── cast             cast_flow · cast_vet · cast_delivery · caster · remux · mirror
│                    · bridge · serve · discovery
├── subs             subs · subalign · srt · oshash · (_bench dev)
├── playback         player · labels · picker · preview · ui · explain
├── discovery/API    api · addons · net
└── foundation       config · types · state/ · log · util · languages
```

## Import-graph discipline

- **Top (few/no internal imports):** `util`, `ui`, `languages`, `types`, `sources`, `net`, `log`
- **Bottom (orchestrator):** `cli` — everything else **never** imports `cli`
- Same tier (no cycles): `headless` ↔ `headless_play` ↔ `cast_flow` ↔ `stream_select` / `cast_vet`
- `cast_vet` → `stream_select` helpers (`_playable_url`, `_cast_playable`, `audio_languages`)
- `availability` is a leaf under selection (probe + denylist); `stream_select` orchestrates targets
- Domain payloads: import from `nstream.types`, not `config`
- State: `from nstream import state` (package public API); stores live in `state.history` /
  `state.cast_session` / `state.dead`

## Domains

### Orchestration

| Module | Role |
|--------|------|
| `cli` | TUI flow + `main` dispatch (not argparse construction) |
| `cli_args` | `build_parser()` — flag surface for TUI and `--json` |
| `headless` | `--json` entry: title select, lifecycle (`--stop`/`--status`/…), delegates play |
| `headless_play` | One-title resolve → prepare_stream → play/cast → success JSON |
| `series` | series-only flow (ADR 0009) |
| `settings` | fzf settings menu |

### Selection & resolve

| Module | Role |
|--------|------|
| `stream_select` | `prepare_stream`: quality → rank/pick → resolve → primary-lang guard |
| `availability` | classified probes, dead-source denylist, drop unusable (ADR 0014/0025) |
| `quality` | parse release names, `HwCaps` (vainfo), rank/filter |
| `tracks` | ffprobe (memoized per url) |
| `sources` | stream-source presets (ADR 0024) |
| `debrid` / `engine` | native debrid resolve / TorrServer P2P |

### Cast stack

```
cast_flow.run_cast
  → cast_vet (audio / video / container)
  → mirror | remux (Tier-2) | caster (Tier-1)
       → cast_delivery.drive_bridge → bridge
```

### Foundation

| Module | Role |
|--------|------|
| `types` | `Meta`, `Video`, `Stream`, `Subtitle`, `HistoryEntry` |
| `config` | `Config`, `PlayOpts`, load/save, path helpers |
| `state/` | history + library, cast session, dead-sources denylist |
| `log` / `util` / `languages` | logging+redaction, atomic I/O, language tokens |

## Naming notes

- **`ui.Caps`** — terminal capabilities  
- **`quality.HwCaps`** — GPU decode capabilities (vainfo)

## When adding a module

1. Place it where the *policy* lives (not the first caller).
2. Preserve import discipline (`cli` at the bottom).
3. Add `tests/test_<mod>.py` (or package tests).
4. Update **this file** only (not CLAUDE architecture prose).
5. New architectural trade-off → ADR.
