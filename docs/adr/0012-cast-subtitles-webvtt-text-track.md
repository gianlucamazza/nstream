# 0012. Cast subtitles as a WebVTT text track on the castbridge path

- **Status:** Accepted
- **Date:** 2026-07-13 (accepted/shipped 2026-07-14)
- **Deciders:** project maintainer

## Context

On the castbridge path, requested subtitles are honestly dropped: the LOAD has no subtitle
field (`bridge._media_load_args`, whose `subtitle` key is display metadata for the TV card,
not a caption track), so `caster.cast`/`remux.cast_file` return `subs_delivered=False`, the
JSON reports `subtitles: null`, and a stderr notice warns the user. The only delivery that
actually carries subs today is the catt fallback (`-s`, one srt). This is correct but
incomplete: the absent-dub safety net ("no ita audio → ita subs") silently loses its value
on the preferred (castbridge) path.

The Default Media Receiver **does** support sideloaded text tracks: per the Google Cast
media protocol, a LOAD can carry `media.tracks` entries of type TEXT with a
`trackContentId` URL — WebVTT is the supported format — and the sender activates them via
`EDIT_TRACKS_INFO`. Everything needed on the nstream side already exists: `serve.py` is a
Range-capable HTTP server already used to feed the DMR (Tier-2), and SRT→VTT conversion is
a small pure-text transform (timestamps `,`→`.` plus a `WEBVTT` header).

## Decision

Extend the castbridge LOAD protocol with an optional `text_tracks` field (list of
`{url, language, name}`), have the daemon translate it to Cast `media.tracks` + an
`EDIT_TRACKS_INFO` activating the first track, and have nstream serve the converted VTT
from `serve.py` (same capability-URL scheme as the Tier-2 file; on direct Tier-1 casts a
dedicated short-lived server instance serves just the VTT). `subs_delivered` then becomes
True on the bridge path when a track was attached, closing the honesty gap with actual
function.

## Rationale

Alternatives considered:

1. **Keep catt-only subs** (status quo) — the preferred sender permanently lacks the
   safety-subtitles feature; users must know to disable castbridge to get subs.
2. **Burn subs into the Tier-2 remux** (ffmpeg subtitle filter) — works for remuxed casts
   only, forces a re-encode of the video (today `-c copy`), and does nothing for Tier-1.
3. **Text track sideload** (chosen) — native DMR capability, no re-encode, works for both
   tiers; the cost is a castbridge daemon change (external repo) plus a VTT converter and
   a serve path in nstream.

## Consequences

- Requires a coordinated change in the castbridge daemon (the `cast` repo's openscreen
  fork): parse `text_tracks`, build `media.tracks`, send `EDIT_TRACKS_INFO`. Until that
  ships, nstream keeps the current honest behaviour (`subs_delivered=False` + notice).
- nstream side (once the daemon supports it): `subs.py` gains an SRT→VTT converter,
  `serve.py` learns to serve a second file (or a second instance serves the VTT),
  `bridge.cast_load` grows the optional field, and `caster.cast`/`remux.cast_file` set
  `subs_delivered` accordingly. All behind a capability check (daemon version/feature
  flag) so old daemons keep working.
- The Tier-1 direct cast gains a firewall implication it didn't have: serving the VTT to
  the TV needs the same inbound LAN rule as Tier-2 (`serve.ensure_firewall`).
- Testing: contract tests on the LOAD payload and the VTT conversion; a field validation
  on the real TV before flipping `subs_delivered` (kb: validate-in-the-field).

## As built (2026-07-14)

Shipped with two refinements over the proposal, both simpler:

- **Activation in the LOAD, not a follow-up `EDIT_TRACKS_INFO`.** The daemon sets
  `media.tracks` (one `TEXT`/`SUBTITLES` track, `text/vtt`) **and** the LOAD's
  `activeTrackIds`, so captions show from the first frame without a second round-trip
  (`cast/native/castbridge/media_receiver_client.cc:SendLoad`).
- **Flat IPC fields, not a `text_tracks` list.** `media-load` gained `subtitleUrl`/
  `subtitleLang`/`subtitleName` (one track — all a movie/episode needs), validated like
  `poster` (`daemon.cc`). `bridge._media_load_args` mirrors them.
- **CORS is mandatory.** The receiver fetches the track cross-site, so `serve.py` sends
  `Access-Control-Allow-Origin: *` on every response (the capability token is the gate).
- **Tier-1 VTT server lifecycle** is a single-slot detached server in `serve.py`
  (`register_sub_server`/`reap_sub_server`), reaped by the next cast / `--stop`; the
  detached-spawn helper (`serve.spawn_detached`) is now shared with the Tier-2 path.
- Field-validated on the real TV (I.S.S., eng track, 1072 cues) before flipping
  `subs_delivered` — the JSON reported `subtitles: eng` (kb: validate-in-the-field).

## References

- Google Cast media protocol: `media.tracks` (TEXT, WebVTT), `activeTrackIds`.
- `src/nstream/bridge.py` (`_media_load_args`), `src/nstream/serve.py`, `src/nstream/subs.py`
  (`to_vtt`), `src/nstream/cast_flow.py`, `cast/native/castbridge/media_receiver_client.cc`.
- ADR 0005/0007/0011; superseded-in-part by **0013** (a custom receiver would render captions
  natively, making the sideloaded-VTT server optional).
