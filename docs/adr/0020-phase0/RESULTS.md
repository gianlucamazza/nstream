# Phase-0 gate results (2026-07-16) — ADR 0020

Bench: `python -m nstream.subalign` (counting proxy, truth comparison, ablation).
Datasets: Coherence tt2866360 (QxR 1080p, 4 093 671 584 B, 758 KB/s; 6 ITA subs in two
timing families 14 s apart, human-validated) and Moon tt1182345 (control, 1.85 GB).

## Sparse remote mode (issue #2 as filed): G3 FAILED — mode is bench-only

- 10 dialogue-dense windows (84 s audio, dev budget 80 MB): flat score landscape
  (peak 0.622 vs runner-up 0.613 with balanced-agreement scoring; 4 scoring variants
  tried). Cross-window vote: no majority. Every candidate refuses. Same on the easy
  control film → the limit is evidence scarcity per window (~8 s), not the film.
- Measured transfer: ~16 MB per 4 s window (fixed per-invocation container cost:
  probe reads + Cues + cluster readahead; proxy-pump inflation included). A reliable
  fingerprint needs 3-4 × 90-120 s windows ≈ **100-300 MB/cast** — the ≤25 MB criterion
  in issue #2 is physically unmeetable on interleaved containers.
- Two engine bugs found and fixed by the gate: fractional `astats reset` silently
  disabling per-frame RMS (cumulative → zero contrast), loopback guard misplaced
  in the engine instead of the orchestrator.

## Full-signal local mode (Tier-2 remux output): gate C PASSED — shipped

- Moon (AAC remux equivalent, one 157-168 s decode pass, 8 virtual segments,
  900 speech spans): original sub **aligned δ=+0.00** (votes 5/8, split 0.10 s);
  synthetic +7 s / −14 s / +33.5 s shifts recovered **exactly** (δ=−7.00 / +14.00 /
  −33.5); foreign-timing subs refused (2/8 votes). align() ≈ 1 s per candidate.
- Coherence (full signal, 1294 spans): **all 6 candidates refuse honestly**
  (votes 3-4/8, split-half disagreement). The film is pathological for VAD
  (wall-to-wall overlapping, largely untranslated chatter). Zero confident-wrong —
  the cardinal contract holds even on the hard case.

## Verdict

Sparse remote alignment: falsified with data → bench-only, documented in issue #2.
Full-signal local alignment (the Tier-2 remux path, where the file is already on
disk): proven → shipped as tier 2 of the selection pipeline, default ON, gated by
the recorded fixtures in `tests/data/` (must-accept + 2× must-refuse).
