# 0053. Coherent EVENT retention, producer health and adaptive startup

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** maintainer
- **Amends:** 0039 (disk retention and initial buffering)

## Context

`live.Producer.on_request` removed files still advertised by an EVENT playlist.
`live.Producer.failed` masked abnormal exits once a partial playlist existed. A detached
HTTP server could remain alive after its FFmpeg child died. Startup buffered a fixed
12 seconds, and disk eligibility estimated a temporal window rather than retained bytes.

## Decision

Keep the finite, progressively produced EVENT presentation already tested on the Philips.
Retain its advertised segments; remove only retired generations or the entire stopped
presentation. Do not introduce a sliding live timeline without receiver acceptance.
`remux.live_refusal` reserves full estimated output with 25% overhead and 512 MiB headroom.

Serialize a byte budget in `live.Job`: known size ×2.5 allows generation overlap during
seek; unknown sizes use the configured remux size cap or 20 GiB when disabled. Clamp it
to initial available space minus 512 MiB. Every two seconds, include partial files and
sidecars in usage and check the remaining reserve. Stop production on either limit.
This sampled guard is not a hard quota and cannot prevent every concurrent-write overshoot.

`live.Producer` publishes private, atomic health records for running, paused, completed
and failed states. A nonzero exit fails even with partial media. A zero exit without
ENDLIST is incomplete. Restart clears the previous error for the new generation.
Published media remains available after failure; playlist requests return 503 instead of
advertising endless growth. Startup checks terminal failure before buffer sufficiency.

The bridge observes producer health on receiver events and socket-timeout ticks. A
followed failure emits an error, ends delivery and never advances to the next episode.
Detached status exposes optional `delivery_error`, separate from receiver errors. No
automatic re-LOAD, language change or quality change hides a failure. An explicit seek
can restart a failed source; disk failures require space or another source.

Initial buffering remains 12 seconds for fast sources. After at least three measured
seconds, production below 1.2× increases the target to max(existing, min(60, 30/rate)),
with rate floored at 0.5 for this calculation. A completed short presentation is exempt.
This absorbs jitter but cannot cure a persistently insufficient source.

## Rationale

EVENT is valid for progressively generated finite media, and preserves the existing
receiver timeline, resume and subtitle behavior. A sliding playlist requires coordinated
media/discontinuity sequences, delayed segment deletion and new TV timeline acceptance.
Keeping EVENT while deleting its media violates its availability contract.

## Consequences

- HLS starts progressively, but disk cost can reach the full generated presentation.
- Ahead pacing remains 30/15 minutes relative to HTTP fetches, not the exact play head.
- Source errors, disk limits and successful completion are distinguishable.
- Health I/O is best-effort and never contains source URLs or raw subprocess errors.
- No history/config migration; old jobs without byte budgets still deserialize.
- `--status` gains an optional delivery-error field; playback fields remain compatible.
- Physical-TV validation of long pauses, adverse networks and seek transitions remains
  separate from offline tests; no new receiver-format compatibility is claimed.

## Verification

Offline verification includes retention beyond the former ten-minute window, partial
failures, incomplete/successful EOF, byte/free-space guards, health-write retries,
slow/fast/short startup, explicit restart, socket-timeout health checks and no advance on
delivery errors. A detached-process experiment confirmed a live HTTP server reports its
failed child (playlist 503, retained segment 200). A real FFmpeg 14-second H.264/AC-3
fixture produced valid AAC-stereo HLS and every advertised segment was readable backwards.

## References

- [RFC 8216 §6.2.1](https://www.rfc-editor.org/rfc/rfc8216.html#section-6.2.1).
- [Apple EVENT construction](https://developer.apple.com/documentation/http-live-streaming/event-playlist-construction).
- [FFmpeg HLS muxer](https://ffmpeg.org/ffmpeg-formats.html#hls-2).
- `live.Producer`, `live.Job`, `remux._await_live`, `serve.RangeFileHandler`,
  `bridge.cast_load`, `cast_delivery.drive_bridge`, `caster.status`.
