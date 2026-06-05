# 0006. Cast realtime via headless mirror (1080p H.264)

- **Status:** Accepted
- **Date:** 2026-06-05
- **Deciders:** project maintainer
- **Implemented in:** `src/nstream/mirror.py` (+ wiring in `cli.py`/`config.py`), and the
  openscreen `cast_sender` (`--audio-sink`, `--playout-delay`).

> Retrospective: decided interactively and validated end-to-end live in one session (TV: Philips
> 43PUS9235). Records the empirical findings so the choice — and the dead ends — aren't re-litigated.

## Context

ADR 0005's Tier-2 remux gives native fidelity but pays a **prepare wait**: the whole Dolby/DTS
file is downloaded+remuxed before playback, because this DMR only plays a complete, Range-served
file (streaming-while-transcoding is empirically black on it). We wanted an **instant-start** cast.

The only true realtime path is to bypass file delivery and use a **Cast Streaming sender**. Full
remoting (original compressed streams → receiver decoder, 4K/HDR/Dolby) is an unfinished C++
effort in the openscreen fork, gated on an unknown (does this TV accept non-Chrome remoting?). So
we took the **mirror** path: mpv decodes locally, the native sender re-encodes to H.264 1080p and
streams over RTP. Instant start; cost = 1080p SDR (no 4K/HDR/Dolby).

A live gate surfaced two real problems a desk-check would have missed:

- **Audio was broken** (drops + A/V desync) *and corrupted mpv's own local playback*. Root cause:
  the sender's **per-app audio tap** (`--audio-pid`) connects to the app's PipeWire output node
  (`PW_KEY_TARGET_OBJECT`, no `CAPTURE_SINK`) → it joins the app's playback graph (active tap),
  causing back-pressure/xruns on the source and bursty, non-monotonic capture timestamps that
  flood the Opus encoder (`MAX_DURATION_IN_FLIGHT` drops + "reference time backwards").
- **Capturing a visible mpv window** is intrusive and occlusion-fragile: a window on a hidden
  workspace stops compositing → capture stalls / switching workspace drops performance.

## Decision

Mirror with this topology (each piece validated live: 0 drops, 0 backwards, clean A/V):

1. **mpv on a headless virtual output** (`hyprctl output create headless` → `HEADLESS-N`,
   1920×1080, fullscreen). Never shown on a physical monitor, always composited, full-res. No
   visible window, no occlusion, no screen taken. (grim confirmed it composites on Hyprland
   0.55.2 — not the black-screen regression seen on 0.52.x.)
2. **Capture by window address** (`window:addr=`), not by output name. The mpv toplevel on the
   headless output is captured directly and reliably.
3. **Audio via a dedicated null sink, captured passively** — a new sender mode `--audio-sink
   <name>` (`CAPTURE_SINK` + `TARGET_OBJECT` = the sink's monitor). mpv is routed to the null sink
   (`--audio-device=pipewire/nstream_cast`); the monitor tap never couples to mpv's graph (no
   source corruption, monotonic timestamps) and the laptop stays silent (movie audio only on TV).
4. **`--playout-delay 500`** — a new sender flag. The hardcoded 120 ms mirror jitter buffer
   starves the audio in-flight budget (~11 frames vs ~110 ms LAN RTT) into constant drops; a
   movie isn't interactive, so a roomy buffer is strictly better.

`mirror.py` orchestrates the lifecycle (create sink+output → launch mpv → resolve addr → launch
sender → track position over IPC) and tears it all down idempotently on exit / `--stop`.

**Backend selection is codec-aware** (`cli.py` `_play_on_cast`/`_auto_play`): the DMR plays AAC
+ HEVC/4K/HDR natively and instantly — strictly better than the mirror (1080p SDR re-encode +
latency/judder). So `--mirror` (or `cfg.cast_mode = "mirror"`) only *actually* mirrors when the
audio is one the DMR can't decode (`vet_cast_audio` → `plan.mode == "remux"`): there mirroring
(mpv decodes Dolby/DTS locally → instant) beats the remux prepare-wait. For decodable audio it is
transparently downgraded to the direct cast (with a stderr note). This makes mirror the
*instant-Dolby* tool, never a regression on AAC releases.

## Rationale / alternatives ruled out

- **Fix the per-app tap to be passive** — impossible without a sink: PipeWire's only passive tap
  is a sink monitor. Hence the dedicated null sink.
- **Capture by output name (`screen:HEADLESS-N`)** — the sender's output-by-name selection is
  **buggy**: `screen_capturer.cc` keeps the *first* bound output (`if (!output_)`) and logs the
  *last* name seen, so it captured the desktop while logging HEADLESS-2. Window-by-address sidesteps
  it. (Latent sender bug, recorded separately; not blocking.)
- **Pinning a visible window** — works but is a workaround: it still occupies a monitor and
  composites real estate. The headless output is the source-clean answer.
- **System-mix default-sink monitor** — clean but plays on the laptop and captures all desktop
  audio. The null sink gives exact isolation + silent laptop.

## Consequences

- Instant cast (no download/remux) for any title, including Dolby/DTS (mpv decodes locally).
- Caps at 1080p SDR H.264 — no 4K/HDR/Dolby passthrough (that needs remoting; out of scope).
- **Judder fix (sender):** the sender originally stamped each captured frame with capture
  wall-clock (`Clock::now()`), so a 24fps movie sampled at the compositor's 60Hz was encoded as
  60fps with 3:2 pulldown baked in → micro-stutter. Fixed by stamping frames with the
  compositor's real presentation time (`Frame::present_nsec`, same CLOCK_MONOTONIC domain):
  repeated captures of one source frame share a present time, so the encoder's monotonic-RTP
  guard drops the duplicate → the stream carries the true 24fps cadence. Validated live (smooth
  after a brief startup transient). Remaining: capture is still output-refresh-driven (on-damage
  capture would save bitrate/CPU); encoder framerate hint still hardcoded 60 (cosmetic under CQP).
- Requires the openscreen `cast_sender` (`$CAST_MIRROR_BIN`), Hyprland, PipeWire; `mirror.available()`
  degrades cleanly to the DMR path otherwise.
- Residual ~2 re-anchor gaps / 12 s from mpv's 200 ms audio buffer → mitigated with
  `--audio-buffer=0.05` (inaudible).
- Latent sender output-by-name bug remains; fixing it would let `screen:` work but is unnecessary
  for this use case.
