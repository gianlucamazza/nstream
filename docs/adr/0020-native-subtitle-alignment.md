# 0020. Native sparse-evidence subtitle alignment and audio arbitration

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** project maintainer
- **Supersedes:** 0019 (entirely); the runtime-fit refinement of 0018 (hash-first
  matching, manual retime and honest reporting from 0018 stand)

## Context

0018's runtime-fit and 0019's windowed-alass oracle were both field-falsified on
2026-07-16 (their post-scripta hold the data; this ADR does not restate it). Exploration
also found the 0019 config surface was DEAD wiring — `load()` never parsed
`sub_autosync*`, so the opt-in was unreachable. GitHub issue #2 asked for a native
integration of the alass alignment concept with sparse full-length audio sampling at
≤ ~25 MB/cast. alass itself is GPL-3.0 (nstream is MIT): the CONCEPT (no-split interval
alignment maximizing an overlap score) is reimplemented from scratch; no GPL code is
read, vendored or linked, and the alass optdepend is removed.

## Decision

**Architecture** — three flat modules, strict tiers (no package):

- `srt.py` (top-tier leaf): the single owner of subtitle TEXT — decode (UTF-8→latin-1),
  retime, to_vtt, cue_spans. Delivery (`caster`/`remux`) uses it directly; it no longer
  imports the selection orchestrator.
- `subalign.py` (leaf: log/util + stdlib): the native engine. `probe()` (sparse remote,
  bench-only) / `probe_local()` (full-signal from a local file) produce a `Fingerprint`
  (windows + speech spans, adaptive per-window hysteresis VAD over an ffmpeg RMS series;
  silencedetect fallback); `align()` is PURE math — balanced-agreement score
  (speech∩cue + silence∩non-cue, coverage-normalized against the runaway-shift failure
  mode), cross-window peak voting (spurious peaks are inconsistent across windows, the
  true δ recurs), hierarchical δ search, and a multi-gate `Verdict` (vote majority, peak
  sharpness vs the best OTHER supported cluster, split-half agreement, drift smear,
  plausibility) with typed refusal reasons. **No fabricated confidence scalar**:
  accepted ⇔ every gate passed. `subsync.py` is deleted (twice-falsified identity).
- `subs.py`: the selection pipeline as explicit evidence tiers.

**Evidence tiers** (auto path):

- Tier 0 — user override: `--sub-offset`/`--sub-fps` or the interactive menu; the
  engine steps aside.
- Tier 1 — protocol hash match (`m == "h"`): synced by construction, no probe.
- Tier 2 — local-media audio alignment (`subs.align_local`, called by `cast_flow`
  after a successful Tier-2 remux): the remux output IS the file the receiver plays,
  so full-signal evidence is free of network cost (~2-3 min decode, within
  `sub_align_budget_s`). Measure-then-apply on the intact original; a refusing
  delivered track falls back to the alternate same-language candidates. Accepted →
  `subtitles_match: "audio"` + `subtitles_offset`.
- Tier 3 — honest guess: first candidate by rank, `"lang"`, `--sub-offset` hint.
- The runtime-fit is deleted; `"runtime"` leaves the vocabulary.

**Phase-0 gate — measured, not asserted** (`docs/adr/0020-phase0/RESULTS.md`):

- The sparse REMOTE mode of issue #2 is **physically unviable at ≤25 MB**: per-window
  fixed container cost ≈ MB-scale; a reliable fingerprint costs 100-300 MB/cast.
  It fails G3 on both datasets and stays bench-only (`python -m nstream.subalign`).
- The full-signal LOCAL mode **passed gate C**: exact offset recovery on the control
  film (δ=0.00; synthetic ±shifts recovered exactly), honest all-refuse on the
  pathological film (wall-to-wall untranslated chatter) — zero confident-wrong on
  every run of the entire Phase 0.
- Recorded fixtures (`tests/data/`: moon_local must-accept incl. synthetic shifts;
  coherence_sparse + coherence_local must-refuse) are the PERMANENT acceptance gate:
  `align()` is pure, so CI replays the field.

**Config**: `sub_align` (default ON — the gate passed before the default was set) and
`sub_align_budget_s` (240), both parsed in `load()` with a round-trip test. The dead
`sub_autosync*` keys are removed (stale entries in user configs are inert).

## Consequences

- Sync stops being a guess wherever a Tier-2 remux happens: verified against the very
  file served, or honestly refused. Direct casts keep hash/lang + the manual lever.
- The cardinal contract — never report sync confidence that wasn't earned — is pinned
  by CI fixtures, not by review vigilance.
- P2P loopback sources skip tier 2 (piece-scheduling guard, as for oshash).
- fps-drift correction remains manual (`--sub-fps`); the engine's drift gate refuses
  rather than mis-correcting. A v2 scale pass needs its own gated dataset.
- Issue #2's ≤25 MB remote criterion is answered with data: unmeetable on interleaved
  containers; the issue closes against this ADR.
