# 0016. Surface the receiver's real track + error state

- **Status:** Proposed
- **Date:** 2026-07-14
- **Deciders:** project maintainer

## Context

nstream reports what it _attached_, not what the receiver _accepted_. After side-loading a
WebVTT caption track (ADR 0012), the `--json` result says `subtitles: eng` because the track
was sent — but nothing confirms the Default Media Receiver fetched the VTT, parsed it, and
activated it. During field validation the only available signals were indirect: the served-file
INFO log latches on the _first_ request per server (`serve.py`), so the VTT fetch (a later
request) is invisible at INFO, and there is no channel carrying the receiver's `activeTrackIds`
or a track-load error back to the CLI. A track the receiver silently rejects (bad content-type,
CORS, VTT parse error) is indistinguishable from a working one.

castbridge already parses `MEDIA_STATUS` from the receiver
(`cast/native/castbridge/media_receiver_client.cc:HandleMediaStatus`) but extracts only
`playerState`/`currentTime`/`duration`/`title` and logs `idleReason`; the status block also
carries `activeTrackIds`, the media's `tracks`, and error events that are currently dropped.

## Decision

Extend castbridge's `MEDIA_STATUS` handling to also read `activeTrackIds` (and any
`LOAD_FAILED`/track error) and forward them through the daemon's `media-status`/`session`
events; nstream consumes them so `--status`/`--follow` report the receiver's **actual** active
tracks and surface a caption/codec rejection instead of silently claiming success. `subtitles`
in the result reflects the receiver's confirmed active track, not just what was sent.

## Rationale

Alternatives considered:

1. **Status quo** — report intent; a rejected track reads as success, and diagnosis needs
   `--debug` archaeology (which we hit).
2. **Poll the served-file access log** — brittle, indirect, and blind to _why_ a track was
   rejected (the receiver, not the server, holds that).
3. **Consume the receiver's own status (chosen)** — the receiver is the source of truth for
   what is active and what failed; reading `activeTrackIds` + error events is the Cast-native,
   correct signal, and it closes the observability gap for audio-track selection (ADR 0013)
   too, not only subtitles.

## Consequences

- **castbridge change** (the `cast` repo fork): parse `activeTrackIds`/`tracks`/error from
  `MEDIA_STATUS`, add them to the pushed status/session JSON. Small, additive, back-compatible
  (absent fields → today's behaviour).
- **nstream change:** `bridge.py`/`cast_delivery.py` carry the new fields; `headless.py`
  reports confirmed active tracks in `--status`/`--follow`; `cast_flow` can flip
  `subs_delivered` on the receiver's confirmation rather than on send, and warn on a rejection.
- Turns the manual "look at the TV" check into a machine-checkable signal — strengthens the
  field-validation gate for 0012/0013 (kb: validate-in-the-field).
- **New failure surface:** a receiver that reports tracks inconsistently → treat the report as
  advisory (never _downgrade_ a working cast on a flaky status), only _upgrade_ diagnostics.
- **Testing:** a castbridge contract test on the extended status payload; nstream unit tests on
  the confirmed-vs-sent `subs_delivered` logic and the `--status` shape.

## References

- Google Cast media protocol: `MEDIA_STATUS.activeTrackIds`, `LOAD_FAILED`/error events.
- `cast/native/castbridge/media_receiver_client.cc` (`HandleMediaStatus`),
  `src/nstream/bridge.py`, `src/nstream/cast_delivery.py`, `src/nstream/headless.py`.
- ADR 0007/0011 (event stream), 0012 (subtitles — the gap that motivated this), 0013 (audio
  track selection benefits too).
