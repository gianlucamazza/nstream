# 0017. Vet the REAL video codec before casting

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** project maintainer

## Context

Casting is vetted asymmetrically: the **audio** decision "always comes from a real ffprobe of
the chosen url — never the release name" (`stream_select.vet_cast_audio`, ADR 0005/0011), but
the **video** codec is trusted from the release-name parse alone — and an _unnamed_ codec gets
the benefit of the doubt (`quality._codec_supported("") → True`, "usually h264/hevc").

Field incident (Coherence, 2026-07-16): the only cached ITA release was a 2013 DivX rip whose
name tags neither codec nor resolution. It passed the cast filter as unknown-codec, the Tier-2
remux copied the video verbatim (`-c:v copy`), and the Default Media Receiver accepted the MP4,
reported `PLAYING` with `receiver_error: null` — and rendered **black**. MPEG-4 ASP is outside
the DMR's decoder set (H.264/HEVC/VP8/VP9). The failure repeated through a second path: with
`--quality 1080` a good H.264 pick was made, then the ITA-dub reselect
(`_reselect_cast_for_lang`) swapped it back to the DivX rip — the reselect walks
`_cast_playable` with no quality or video vetting.

ADR 0016 observability cannot catch this class: the receiver does not consider it an error.
Selection time is the only defense.

## Decision

1. **Name the legacy tokens** (`quality._parse_codec`): XviD/DivX/MP4V/MPEG-4 → `mpeg4`,
   MPEG-1/2 → `mpeg2`, VC-1/WMV → `vc1`, checked _after_ the modern tokens ("MPEG-4 AVC"
   stays h264). A named legacy codec is excluded by every caps profile (like AV1 on cast)
   instead of enjoying the unknown-codec benefit of the doubt.
2. **Probe-verify the video before casting** (`stream_select.vet_cast_video`): read the first
   video stream's real codec from the same memoized ffprobe the audio vetting uses (zero extra
   network; `tracks.Tracks.video_codec`). Unknown (unprobeable) keeps the benefit of the doubt,
   mirroring the audio stance. If the codec is outside `quality.CAST_VIDEO_DECODABLE`
   ({h264, hevc, vp8, vp9} — AV1 matches `cast_caps`): reselect the best castable candidate,
   else fall back to the **mirror** (mpv decodes locally — the ADR 0006 "what even the DMR
   can't play" path), else fail explicitly (`CastVideoUnsupported` →
   headless `video_codec_unsupported`) instead of casting black.
3. **Guard the language reselect**: `_reselect_cast_for_lang` skips candidates whose probed
   video the DMR can't render — silent-wrong-language must not be traded for a black screen.

## Consequences

- A black cast becomes: another release, or the mirror (with an explanatory notice), or an
  explicit `video_codec_unsupported` error carrying the codec. Never `PLAYING` + black.
- The video vetting costs no extra probe on the happy path (memoized with the audio probe);
  a reselect probes at most `probe_cap` (4) candidates.
- Known gap (follow-up, not this ADR): the language reselect still ignores the per-session
  `--quality` choice — it can legally return a different resolution than the exact filter
  picked. Video decodability is corrected here; quality parity is a separate decision.
