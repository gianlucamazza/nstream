# 0039. Tier-2 audio conversion streams live HLS-TS instead of a complete file on disk

- **Status:** Accepted (2026-10-01)
- **Date:** 2026-10-01
- **Deciders:** maintainer
- **Amends:** 0005 (the delivery format of Tier-2; the decision to remux on the host stands)

## Context

ADR 0005 delivers a Tier-2 cast as a complete MP4 on disk. Its tests showed that streaming
variants give a black screen on this receiver:

- on-the-fly fMP4;
- `--stream-type live`;
- HLS-fMP4;
- a growing file served with Range.

The cost of the complete-file approach is the whole download before the TV starts, plus
disk equal to the release size. That cost caused the 2026-10-01 incident: a 66 GB release
with 52 GB free (ADR 0036).

HLS with **MPEG-TS** segments was never tested. A matrix run on the target TV on
2026-10-01 (`0039-phase0/receiver-matrix-2026-10-01.md`) found:

- **Dolby in MP4 stays silent.** AC-3 and E-AC-3 play muted even in MP4. A container rewrap
  is not enough, so an AAC conversion stays necessary. This falsifies the hypothesis, taken
  from Google's passthrough documentation, that Dolby only needs a rewrap.
- **Complete HLS-TS works.** HLS-TS with H.264 copy and AAC audio plays with sound.
- **Growing HLS-TS works.** An EVENT playlist that grows while ffmpeg converts plays with
  sound, for both **H.264 and HEVC** video copied untouched.

## Decision

When a cast needs audio conversion (or an `.mkv` rewrap), nstream runs
`ffmpeg -c:v copy -c:a aac -f hls -hls_segment_type mpegts -hls_playlist_type event`
into a bounded per-cast directory. It serves that directory with the existing
token-and-CORS server, and casts the playlist as soon as the first segments exist.

- The complete-file remux (ADR 0005) remains the fallback when the live variant fails to
  start. ADR 0036's feasibility check then only needs the disk for the segment window, not
  the release size.
- **Seeking** is out of scope until it is tested. The receiver plays an EVENT playlist
  forward. A seek beyond what has been produced restarts ffmpeg at `-ss` with a new
  playlist and a new LOAD at the start time.

## Rationale

| Option                            | Start delay    | Disk           | Status                           |
| --------------------------------- | -------------- | -------------- | -------------------------------- |
| Complete MP4 (ADR 0005)           | whole download | release size   | works                            |
| Rewrap Dolby → MP4                | whole download | release size   | **muted** on this TV (falsified) |
| Live HLS-fMP4 / growing MP4       | seconds        | small          | black (ADR 0005)                 |
| **Live HLS-TS, video copy + AAC** | seconds        | segment window | **works** (H.264, HEVC 8-bit)    |

## Consequences

- Removes the multi-GB prepare wait and most `remux_infeasible` outcomes.
- **New gates before acceptance**, each one recorded in `0039-phase0/`:
  1. 4K HEVC Main10 with HDR10;
  2. a real debrid source as ffmpeg input;
  3. playback across the full runtime with no stall;
  4. the castbridge sender path;
  5. side-loaded WebVTT subtitles alongside the HLS LOAD.
- The finish predicate and resume (ADR 0029) must handle `duration: -1` on a live
  playlist. The duration can come from the probe instead.
- Segment cleanup joins the detached-server lifecycle: idle-exit, `--stop` and GC.

## Acceptance (2026-10-01)

Gates 2–5 passed on the 43PUS9235 (`0039-phase0/receiver-matrix-2026-10-01.md`): real debrid
input, 62 min to the end of the film with 0 stalls, the castbridge BUFFERED path with seek
inside the produced range, side-loaded WebVTT. Gate 1 (4K HEVC Main10) is still open: a 4K
live start that fails falls back to the complete file, which already handles 4K.

Decisions taken at acceptance:

- **Stereo AAC 192k** on the live path. AAC 5.1 in HLS stalls this receiver (matrix #7/#8);
  a stereo AAC source track is copied. The complete-file fallback keeps channel-aware AAC.
  `cast_live: false` restores the complete file for anyone who prefers surround to a fast
  start.
- **Disk:** the producer is not throttled by rate but by distance. `live.Producer` deletes
  segments more than 10 min behind the newest segment the receiver requested, and pauses
  ffmpeg (SIGSTOP) 30 min ahead of it, resuming at 15 min. No receiver polling: the
  segment requests are the play head. Feasibility (ADR 0036) checks this window, not the
  release size; a live failure falls back to the complete file, and a complete file the
  disk refused is `remux_infeasible` — never a mute cast.
- **Seek:** inside the produced range and the 10-min window behind, it works; a backward
  seek past the window is not served, a forward one waits for the producer. The
  restart-at-`-ss` design stays deferred.
- **Resume:** the producer starts from 0 and the LOAD seeks to the resume point once the
  playlist covers it (timestamps stay absolute, so position, history and subtitles need no
  offset). A far resume point over a slow debrid costs that production time.
- The debrid url reaches the detached producer on stdin, never in argv; ffmpeg reads a
  `urlproxy` loopback url that resumes dropped upstream reads with a Range.

## Field results after acceptance (2026-10-01, appended)

On the TV, from `fix`/`feat` commits on top of the acceptance:

- **Gate 1 passed:** Blade Runner 2049, 3840×1600 HEVC Main 10 HDR10 (smpte2084/bt2020),
  ita AC-3 → AAC stereo, live start in 18 s, picture and sound confirmed by the viewer.
- **Custom receiver:** the custom app (`cast_receiver_app_id`) answers an HLS LOAD with
  `LOAD_FAILED` without fetching the playlist; the live LOAD always uses the Default Media
  Receiver.
- **Seek:** the receiver honours short seeks exactly (±30–60 s) but clamps far forward ones
  (+90 → +35, +180 → +5); a LOAD with a start offset lands exactly. `remux.live_seek`
  re-LOADs for jumps over 30 s, waiting for the producer when the target is within its
  pacing reach (75:17 reached in 29 s from 58:40).
- **Resume (supersedes the "producer starts from 0" decision above):** producing up to a
  50-min resume point took 223 s. The producer now opens the source at the resume point
  (`-ss`, `-noaccurate_seek`, `-copyts`): start in 27.7 s end to end. The receiver counts
  the playlist from 0 whatever the PTS (tested with and without `-output_ts_offset`), so
  the first segment's start PTS — the keyframe, byte-identical to the source frame, audio
  +6 ms — is the offset nstream adds to positions, seeks and the caption track.
- **History:** a live playlist reports duration −1, which history rejected; the probed
  duration is kept in the live state.

## References

ADR 0005, 0015, 0022, 0029, 0031, 0035, 0036. Symbols: `remux.remux_to_file`,
`remux.cast_file`, `serve.spawn_detached`, `cast_flow.run_cast`. External: Google Cast
supported media — HLS with MPEG-TS segments and AAC audio.
