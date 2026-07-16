# 0019. Audio-anchored subtitle sync (alass) when no hash match exists

- **Status:** Proposed
- **Date:** 2026-07-16
- **Deciders:** project maintainer

## Context

ADR 0018 established hash-first selection with a runtime-fit fallback. Field evidence
(Coherence, 2026-07-16) showed the fallback's limit: the six ITA tracks form two timing
families **14 s apart** (measured by cue-text anchor matching across the whole film); the
runtime-fit chose the one whose LAST cue matches the file duration to 1 s — and it played
**~14 s late** (user-confirmed). End-point alignment is weak evidence: credits padding can
make the wrong family "fit". With no protocol hash match (`m == "h"` — rare in practice)
there is **no ground truth in the metadata at all**: only the media's own audio can
arbitrate.

The manual `--sub-offset` remedied it in one recast, but requires the user to quantify
the shift by ear.

## Decision

When the subtitle pick is not a protocol hash match and the `alass` binary is present
(optdepend, Rust, packaged; community benchmarks favour it over ffsubsync), verify and
correct the chosen SRT against the media's real audio:

1. Extract a bounded reference segment with the existing `ffmpeg` dependency:
   `ffmpeg -t {window} -i <url> -vn -ac 1 -ar 8000 ref.wav` (default window ~900 s,
   ≈ 14 MB WAV; ranged HTTP read, no full download).
2. Run `alass --no-split ref.wav chosen.srt corrected.srt` — constant-offset mode only:
   with a partial reference, split detection could mangle cues beyond the window, while a
   constant offset (the live failure class) extrapolates safely.
3. On success, deliver the corrected file and report `subtitles_match: "audio"`; log the
   detected offset. On any failure/timeout (~30 s cap) fall back to today's behaviour and
   reporting — best-effort like every other external tool.
4. Config: `sub_autosync` (default on when alass is present), `sub_autosync_window_s`.

Runtime-fit remains as tie-breaker BEFORE the audio pass (picks the candidate to correct)
and as the only fallback without alass.

## Consequences

- Sync stops depending on crowdsourced metadata quality: the media itself is the anchor.
  A wrong-family pick becomes a corrected file, not a user-visible 14 s lag.
- Cost per cast without a hash match: one bounded ffmpeg read + one alass run (seconds).
  Zero cost when a real hash match exists or alass is absent.
- New optdepend (alass) — consistent with the external-CLI philosophy (mpv/ffmpeg/catt).
- Drift (fps) correction stays manual (`--sub-fps`): detecting it reliably needs a
  reference longer than the bounded window; revisit only if the field shows drift cases.
