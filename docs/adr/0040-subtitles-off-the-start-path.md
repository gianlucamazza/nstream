# 0040. Subtitles stay off the cast start path: embedded first, alignment after start

- **Status:** Proposed
- **Date:** 2026-10-01
- **Deciders:** maintainer
- **Amends:** 0020 (when the audio alignment runs; its engine and gates stand)

## Context

ADR 0020 aligns a "lang"-tier subtitle against the audio of the local Tier-2 remux.
`cast_flow.run_cast` runs `subs.align_local` **after** `remux.remux_for_cast` and
**before** `remux.cast_file`. The 2026-10-01 fixes made the alignment work: it had failed
5 times out of 5 since July. The same fixes made it visible on the start path. On the Gatsby
remux (3.4 GB, 142 min) it adds **41.8 s** to the time before the TV starts, mostly spent
demuxing a file the remux had just written.

Three facts constrain the next step:

- **ADR 0039 removes the local file.** The Tier-2 cast becomes live HLS-TS, which starts
  after a few segments. There is no complete file to align against before the start. A
  pre-start alignment would cancel the latency ADR 0039 buys.
- **Many releases already carry a synced text track.** `tracks.probe_tracks` already lists
  `subs` (subrip/ass/mov_text, with language and disposition). nstream never uses them on
  the cast path. It downloads from OpenSubtitles and then measures sync that an embedded
  track has by construction.
- **No sync control once the cast starts.** `--sub-offset` exists only before the start.
  Today the only way to fix a late track is to stop and recast. The field test on
  2026-10-01 hit exactly this.

## Decision

nstream never delays a cast start for subtitles.

1. **Embedded text track first.** When the chosen release carries a text subtitle stream in
   the wanted language, nstream extracts it (`-map 0:s:N -c:s webvtt`), in the remux/HLS
   ffmpeg pass when there is one. It becomes a new tier, `subtitles_match: "embedded"`,
   above `hash`. The tier prefers a `forced` track only when the audio is already in the
   user's primary language. Bitmap tracks (PGS/VobSub) do not qualify.
2. **Alignment after the start.** The "lang"-tier track is cast immediately and reported as
   `subs_unverified`. Speech activity (the `astats` RMS chain of `subalign._mono_chain`) is
   computed in the **same ffmpeg pass** that produces the remux or the HLS segments. No
   second decode is run. When `subalign.align` accepts an offset, the served WebVTT is
   rewritten in place and the receiver is made to re-read it (see Phase 0). The JSON and
   the `--status` output then report `audio` and the offset.
3. **In-cast offset control.** `nstream --sub-shift ±S` works on the active cast. It retimes
   the served WebVTT through the same re-read mechanism, so a manual correction no longer
   costs a recast.

Drift/fps correction (ADR 0020 v2) stays out of this ADR. It needs its own ground truth.

## Rationale

| Option                                            | Start latency                         | Works with ADR 0039                   | Cost                                               |
| ------------------------------------------------- | ------------------------------------- | ------------------------------------- | -------------------------------------------------- |
| Status quo (align before start)                   | +40 s per Tier-2 cast                 | No: needs the full file               | none                                               |
| Align in parallel with the remux                  | +0 s only if the remux finishes later | No                                    | double decode, CPU contention with ffmpeg          |
| **Embedded first + align after start (this ADR)** | +0 s                                  | Yes: RMS comes from the producer pass | re-read mechanism, tier plumbing                   |
| Drop audio alignment                              | +0 s                                  | Yes                                   | back to unverified tracks (the 2026-10-01 symptom) |

Extracting an embedded track is free: the bytes pass through ffmpeg anyway. It is also the
only source that is correct by construction, which is stronger evidence than hash or
audio. Moving the RMS into the producer pass removes the 41.8 s demux.

## Consequences

- `SubsPick.match` gains `"embedded"`. `headless.md` documents it, and `subs_unverified` can
  turn into `audio` during a `--follow` run, signalled by an event line.
- The served WebVTT becomes mutable for the life of a cast. The detached subtitle server
  (`serve.persist_sub`) already opens it per request, so a rewrite is atomic
  (`util.atomic_write`).
- `remux` and the ADR 0039 producer gain a second output: an RMS series, and optionally the
  extracted WebVTT. ffmpeg argv construction must stay token-free (`urlproxy.local_url`).
- **Phase 0 (before code), on the 43PUS9235:**
  - **Gate 1, re-read without a new LOAD.** Rewrite the served VTT, then seek to the current
    position. Pass if the receiver re-fetches the VTT (server log) and renders the new
    timings. Otherwise test an `EDIT_TRACKS_INFO` toggle (track off/on). The last resort is
    a re-LOAD at the current position, and Phase 0 measures its interruption.
  - **Gate 2, RMS from a tee in the remux pass.** The tee adds less than 10 % to the remux
    wall time. Its RMS matches `subalign.probe_local` on the same file (same `align`
    verdict on the recorded fixtures).
  - **Gate 3, embedded extraction.** On 5 releases with an embedded ita/eng text track, the
    extracted VTT renders in sync on the TV.
- If gate 1 fails in all three variants, point 2 falls back to aligning before the start
  only for the full-file path (ADR 0005), with the budget kept. Point 3 then also needs a
  re-LOAD.

## References

- ADR 0018 (subtitle evidence tiers), ADR 0020 (alignment engine), ADR 0039 (live HLS-TS
  Tier-2), ADR 0016 (receiver track observability).
- `subs.align_local`, `subs.report_unverified`, `subalign.probe_local`, `subalign._mono_chain`,
  `cast_flow.run_cast`, `remux.remux_for_cast`, `serve.persist_sub`, `tracks.probe_tracks`.
- Field timing 2026-10-01: Gatsby remux alignment 41.8 s, offset +0.06 s.
