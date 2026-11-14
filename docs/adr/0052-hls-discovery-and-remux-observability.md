# 0052. Discover installed HLS senders and explain complete-file preparation

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** maintainer

## Context

An 11.3 GB MKV needed complete-file preparation while `cast_live` was enabled.
`bridge.bridge_available` returned false: discovery supported an override and a historical
build path, but not installed executables on PATH. Selection, prefetch and delivery also
repeated different subsets of live eligibility checks. Progress showed only a percentage.

## Decision

`bridge._binary` resolves an explicit `CASTBRIDGE_BIN`, then PATH, then the historical
build tree. An invalid explicit override does not fall through. No automatic installation.

`remux.live_requirement_refusal` owns local prerequisites. The read-only doctor uses it;
`remux.live_unavailable_reason` adds source and disk eligibility and is shared by selection,
prefetch and delivery. Runtime producer/head/receiver failures retain the complete-file
fallback. The prepare message names eligibility refusal or unsuccessful live startup.

`remux._await_ffmpeg` reads output size and processing speed as well as media position.
ETA requires a known duration, five elapsed seconds and three positive finite speed samples
within a 1.25 ratio. ETA excludes finalization and disappears at end or with unstable data.
Logs contain total elapsed time and last-advancement-to-exit interval, without source URLs.

## Rationale

Restore access to the existing HLS implementation before changing delivery formats or
download concurrency. Keep processing measurements separate from `state.throughput`:
encoded output bytes per second do not measure incoming network bandwidth.

The installed catt 0.13.3 forwards `stream_type` and media metadata, so an explicit
BUFFERED HLS experiment is justified. This does not establish receiver compatibility.
Production HLS remains castbridge-only until the hardware acceptance matrix passes.

## Consequences

- Installed castbridge binaries become discoverable without private build paths.
- Local doctor success certifies prerequisites only, not a playable source or receiver.
- Prefetch now respects dual-layer and disk guards before starting a producer.
- Complete-file remux retains video copy, channel-aware AAC and `+faststart`.
- No playback JSON fields, state schemas, runtime dependencies or audio defaults change.
- The optional `cast_live` doctor check is additive to the existing checks array.
- Last-advancement-to-exit includes scheduling/progress reporting and possibly other I/O;
  it cannot be described as an exact faststart duration or proof of the bottleneck.

## References

- ADR 0039, 0044, 0050.
- [Google Cast supported media](https://developers.google.com/cast/docs/media).
- [Cast MediaInfo](https://developers.google.com/cast/docs/reference/web_sender/chrome.cast.media.MediaInfo).
- [FFmpeg MOV/MP4 muxer](https://ffmpeg.org/ffmpeg-formats.html#mov_002c-mp4_002c-ismv).
- `bridge._binary`, `remux.live_feasible`, `remux.prefetch_live`, `remux.cast_live`,
  `remux._await_ffmpeg`, `doctor.inspect`.
