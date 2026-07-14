# 0013. Custom Cast (CAF) receiver with runtime codec capability probing

- **Status:** Proposed
- **Date:** 2026-07-14
- **Deciders:** project maintainer

## Context

nstream casts to the **Default Media Receiver** (`CC1AD845`,
`cast/native/castbridge/media_receiver_client.h:app_id`). That receiver drives a whole class
of costs:

- **Dolby/DTS is silent** (AC-3/E-AC-3/DTS/TrueHD), so `remux.py` (ADR 0005) downloads the
  whole file and transcodes audio to AAC before playback — a multi-GB fetch and a prepare
  wait. Observed twice in one evening (I.S.S., Project Hail Mary): the 4K Dolby pick forced a
  30-60 GB remux.
- **It cannot switch embedded audio tracks**, so `stream_select.vet_cast_audio` remuxes just
  to _select_ a dub (`docs/selection.md`), and the whole `CastAudioPlan` machinery exists to
  work around it.
- **It has no native subtitle UX** beyond a sideloaded VTT track (ADR 0012); styling and
  multi-track selection are not exposed.

Codec support on Cast is **hardware-dependent**, not receiver-fixed: the Web Receiver SDK
supports AC-3/E-AC-3 **passthrough** to a Dolby-capable display, and a receiver can query it
at runtime with `CastReceiverContext.canDisplayType()`. The Default Media Receiver simply does
not enable passthrough or expose these controls. The TVs nstream targets (e.g. the Philips
Android TV at 192.168.1.228) generally decode Dolby Digital themselves — so the silence is a
**receiver-app limitation, conditional on the display**, not an absolute device limit.

## Decision

Ship a minimal **CAF v3 Web Receiver** (a static HTML/JS app hosted over HTTPS under a
registered application id) that: enables AC-3/E-AC-3/(DTS where the device allows) passthrough,
exposes audio-track and text-track selection, and **probes the live device** with
`canDisplayType()` at session start, reporting the verdict back to castbridge. `castbridge`
LOADs to this app id instead of `CC1AD845`. nstream's Tier-2 remux and the select-a-dub remux
become a **capability-gated fallback**: used only when the receiver reports it cannot play the
release's real audio — never deleted (kb: run-disabled-not-removed), because non-passthrough
displays still need it.

## Rationale

| Option                           | Dolby without remux                         | Audio-track switch   | Sub UX          | Cost                                                                                       |
| -------------------------------- | ------------------------------------------- | -------------------- | --------------- | ------------------------------------------------------------------------------------------ |
| Stay on DMR (status quo)         | no (always remux)                           | no (remux to select) | sideload only   | zero, but the remux tax is permanent                                                       |
| **Custom CAF receiver (chosen)** | yes, where the display supports it (probed) | native               | native + styled | app-id registration, HTTPS hosting, a JS receiver, castbridge app-id + capability plumbing |
| Burn everything via mirror       | yes (mpv decodes)                           | yes                  | yes (burn-in)   | 1080p SDR re-encode + latency for _every_ cast (ADR 0006) — too blunt as the default       |

The custom receiver is the only option that removes the remux tax **without** downgrading
quality, and it does so **honestly**: `canDisplayType()` gates each codec on the real device,
so we never assume Dolby works — we ask. This is the modern Cast-native path (Web Receiver SDK
v3), not a workaround layered on the default app.

## Consequences

- **New hosted artifact:** a CAF receiver (static HTML/JS) served over HTTPS, plus a Google
  Cast **application id** registered to it. An operational dependency (the receiver URL must
  stay reachable) — mitigate by hosting it on the same infra as the rest of the stack and
  pinning a fallback to `CC1AD845` when the custom id fails to launch.
- **castbridge changes** (the `cast` repo fork): a configurable `app_id`, the launch/handshake
  already generalises, plus a channel to receive the receiver's `canDisplayType()` capability
  report and surface it (feeds ADR 0016 observability).
- **nstream changes:** `remux.needs_remux` / `vet_cast_audio` consult the receiver capability
  instead of a hardcoded codec set; the Tier-2 and select-dub remux paths stay but fire only on
  a negative probe. `quality.py` cast ranking stops penalising Dolby when the device passes it.
- **New failure mode:** a receiver that launches but mis-probes (claims a codec it then can't
  render) → guard with the receiver's `ERROR`/`idleReason` already surfaced by castbridge, and
  fall back to remux on a mid-play codec error.
- Sub delivery (ADR 0012) can migrate to the receiver's native text-track UI, making the
  sideloaded-VTT server optional — but the VTT path stays as the fallback for `CC1AD845`.
- **Testing:** a receiver unit/integration harness (canDisplayType matrix), a castbridge
  contract test for the app-id + capability channel, and field validation on a Dolby-capable
  and a non-Dolby display before flipping the default app id (kb: validate-in-the-field).

## Status note — enabling plumbing shipped, decision still gated (2026-07-14)

The ADR stays **Proposed**: the decision (custom receiver as the Dolby path) can't be _realized_
or field-validated without two things only an operator can provide — a **registered Google Cast
application id** (paid, account-bound) and **HTTPS hosting** of the receiver. What was safely,
testably completable _without_ those was shipped **dormant** (kb: run-disabled-not-removed), so
the remaining work is exactly the operator steps, not code:

- **castbridge** (`cast` repo): `media-load` accepts an optional, validated `appId`
  (`daemon.cc::IsOptionalAppId`), forwarded to `MediaReceiverClient::app_id()` — empty →
  `kDefaultMediaReceiverAppId` (`CC1AD845`, back-compat). Built clean, native tests pass.
- **nstream**: `bridge._media_load_args(app_id=...)` forwards it; **no nstream path sets it yet**
  (default receiver unchanged). Tested (`test_media_load_args_app_id`).
- **receiver artifact + runbook**: `cast/native/receiver/index.html` (a minimal CAF v3 receiver
  that probes the device with `canDisplayType()` on READY and broadcasts a `capabilities` message
  on `urn:x-cast:it.gianlucamazza.castbridge`) + `cast/native/receiver/README.md` (the operator
  steps). **Not field-validated** (no registered id).
- **Deliberately NOT built:** the nstream capability-gating half (remux consulting the receiver
  capability) — it depends on a live receiver reporting back, which can't be validated without the
  registration/hosting, so building it now would be untested, unvalidatable code (anti-theater).
  The capability channel exists; the consuming policy waits for a real receiver.

## References

- Google Cast: [Supported Media](https://developers.google.com/cast/docs/media),
  Web Receiver SDK (`CastReceiverContext.canDisplayType`, `PlaybackConfig`), AC-3 passthrough.
- Issue precedent: EC-3/AC-3 not played by the default receiver
  ([issuetracker 69227108](https://issuetracker.google.com/issues/69227108)).
- `cast/native/castbridge/media_receiver_client.*`, `src/nstream/remux.py`,
  `src/nstream/stream_select.py` (`vet_cast_audio`), `src/nstream/quality.py`.
- Supersedes-in-part ADR 0005 (Tier-2 remux → fallback), 0012 (subtitles → native); relates to
  0006 (mirror), 0016 (receiver observability).
