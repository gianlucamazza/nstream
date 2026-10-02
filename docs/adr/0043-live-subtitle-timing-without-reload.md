# 0043. Live subtitle timing changes never reload the media

- **Status:** Accepted (2026-10-02, plan approved by the maintainer)
- **Date:** 2026-10-02
- **Deciders:** maintainer
- **Revises:** 0042 point 4; 0040 point 3 (in-cast shift)

## Context

ADR 0042 kept a downloaded (OpenSubtitles) track side-loaded on live casts, and applied the
after-start alignment and `--sub-shift` to it through a re-LOAD at the current position.
A side-loaded WebVTT is fetched once at LOAD, so a new timing needed a new LOAD.

Field test on 2026-10-02 (In the Mood for Love, live, embedded ita rendition):

- `--sub-shift` broke playback. The first time the TV went IDLE ("media went IDLE
  (INTERRUPTED)" twice). The second time it stuck in BUFFERING.
- castbridge made every LOAD a new session. `MediaController::LoadAsync` reset the client,
  opened a new TLS connection and LAUNCHed the receiver app again, which interrupted the
  playing media.

A subtitle timing change has nothing to do with the media. Reloading costs a rebuffer and
the receiver state.

## Decision

1. **Every live subtitle is a rendition.** A downloaded track is the producer's second
   input:
   - it is a cleaned WebVTT (`remux._live_sub_input`), trimmed per generation to the cues
     still running at its `ss_s` (`live._write_sub_input`);
   - with `-copyts` its cues stay in film time.

   The live path no longer side-loads anything. Complete-file and direct casts keep
   side-loading, where it is the native mechanism.
2. **Timing changes only write files.**
   - `remux.live_sub_shift` writes the manual total, and the alignment writes `align.json`.
   - serve adds both to each subtitle segment as the receiver fetches it
     (`serve._serve_shifted_vtt`).
   - Already-fetched segments keep their timing, so a change shows within the receiver's
     fetch-ahead.
3. **castbridge (≥ 0.4.2) LOADs on the live session.** A LOAD for the same device and
   receiver app, while that app is still the one running, goes on the existing media
   channel: no reconnect, no LAUNCH (`CanReuseSession`, `MediaReceiverClient::Reload`).
   The late statuses of the replaced media are dropped, so its INTERRUPTED end does not
   read as the session ending. The live seek and restart LOADs ride this path.

## Rationale

| Option | Verdict |
| ------ | ------- |
| Re-LOAD at the current position (0042 point 4) | Rejected: interrupts the media; with castbridge ≤ 0.4.1 it relaunched the app and left the TV IDLE or BUFFERING |
| Side-loaded VTT + runtime `EDIT_TRACKS_INFO` toggle to refetch | Not needed: gate 2 passed without it; kept as the fallback if a receiver buffers text far ahead |
| Rendition for every live track, shift on fetch | Chosen: one mechanism, the media untouched |

Phase 0 (2026-10-02, 43PUS9235):

1. **Fetch window.** The receiver fetches VTT segments 20–25 s ahead of the position.
2. **Shift without LOAD.** `5` written to the shift during playback. The viewer saw the
   cues move later, with no stop.
3. **Downloaded SRT as a second input.** With a fast resume at 600 s and `-copyts`, the 47
   cues in the window equal the file's to the millisecond. An `-ss` on the subtitle input
   does not trim it: every earlier cue still came through, hence the per-generation trim.
4. **castbridge reused session.** A LOAD during playback causes no LAUNCH and no
   INTERRUPTED. Passed with castbridge 0.4.2: one `launching media app` per cast.
   - A +300 s seek (re-LOAD) played on at 332 s.
   - Seeks to 3000 s and back to 1500 s (producer restarts) played on with the text track
     active.

   The first restart exposed that the producer removed the old generation before the new
   LOAD landed: the TV went IDLE (ERROR) for a few seconds, which the old relaunch had
   masked. A retired generation now goes at the receiver's first request of the new one
   (`live.Producer.on_request`).

## Consequences

- `live.Job.sub_file` joins the job contract. The trimmed input `sub<gen>.vtt` lives in
  the live dir and goes with its generation.
- `live.Aligner` reads only the rendition segments, never a side-loaded file.
- A timing change takes up to the fetch-ahead (~25 s) to show. That is accepted, since
  the media never stops.
- A live cast needs a subtitle language for castbridge to activate the rendition. `und` is
  used when none is known.

## References

ADR 0016, 0039, 0040, 0042. Symbols: `live.producer_cmd`, `live._write_sub_input`,
`remux.cast_live`, `remux.live_sub_shift`, `remux.live_alignment`, `serve.sub_shift`;
castbridge `MediaController::LoadAsync`, `MediaReceiverClient::Reload`.
