# 0042. Embedded text subtitles first, delivered as an HLS rendition on the live path

- **Status:** Proposed
- **Date:** 2026-10-02
- **Deciders:** maintainer
- **Revises:** 0040 (its points 1–3, for the live delivery of ADR 0039)

## Context

ADR 0040 proposed using embedded text subtitles first, aligning after the start, and an
in-cast `--sub-shift`. Two facts from 2026-10-01 change how to do it.

- **The tracks are there.** The ita release of "Eternal Sunshine" carried embedded `subrip`
  tracks in ita and eng (`tracks.probe_tracks`). OpenSubtitles had neither language, and
  nstream reported "nessun sottotitolo".
- **The delivery is live.** ADR 0039 casts a growing HLS playlist, so no complete file
  exists. Extracting a whole embedded track would read the whole release, since MKV
  interleaves subtitle packets with the media. A side-loaded WebVTT is fetched once at LOAD.
  `subs.align_local` cannot run either, because it needs the complete file.

ffmpeg can segment the subtitle stream into an HLS **WebVTT rendition** next to the media
(`-var_stream_map "v:0,a:0,s:0,sgroup:subs,language:ita"`, `-master_pl_name`). Checked
locally on 2026-10-02: a master playlist with `EXT-X-MEDIA:TYPE=SUBTITLES`, a segmented
`v0_vtt.m3u8`, and cues in film time when `-copyts` is used. The segments carry no
`X-TIMESTAMP-MAP`, and the cue text is passed through raw (`<- Clem:`).

## Decision

1. **Embedded first.** `subs.embedded_pick` chooses an embedded text track (subrip, ass,
   mov_text or webvtt; never PGS/VobSub) in the wanted language. It prefers `forced` only
   when the audio is already in the primary language. The tier is `subtitles_match:
"embedded"`, above `hash`: synced by construction.
2. **Live:** the producer writes the subtitle rendition beside the media playlist, and the
   cast LOADs the master playlist. castbridge activates the text track, either through LOAD
   `activeTrackIds` or a new `edit-tracks` control in the cast repo.
3. **Complete file:** the remux pass extracts the same track (`-map 0:s:N -c:s webvtt`) and
   side-loads it (ADR 0040 point 1).
4. **Downloaded subtitles on live:** a downloaded (OpenSubtitles) track keeps the side-loaded
   VTT. Alignment runs after the start from an RMS tee in the producer pass and applies
   through a re-LOAD at the current position. `--sub-shift ±S` works the same way.

## Phase 0 gates (TV, before code)

1. The master playlist with the rendition plays on the Default Media Receiver (BUFFERED
   LOAD).
2. The text track shows in sync once active, including after a fast-resume `-ss`
   (`-copyts`) and after a re-LOAD seek. Test both as ffmpeg writes it and with an
   `X-TIMESTAMP-MAP` stamped on the segments.
3. Check whether `DEFAULT=YES` alone activates it, or castbridge must.

If gate 1 or 2 fails, the live path side-loads the downloaded VTT only, and embedded
tracks serve the complete-file path.

## Phase 0 results (2026-10-02, 43PUS9235)

- **Gate 1 passes only with `CODECS`.** The master playlist ffmpeg writes has no `CODECS`
  attribute. Without it the receiver fetches `master`, `v0.m3u8` and two segments, then goes
  IDLE; this holds also without any subtitle rendition. The source is HEVC Main, so the
  receiver presumably assumes H.264. With
  `CODECS="hvc1.2.4.L120.B0,mp4a.40.2"` written into `EXT-X-STREAM-INF` it plays: the viewer
  confirmed the picture. nstream must write the master itself, deriving `CODECS` from the
  probe.
- **Gate 3 fails without castbridge.** `DEFAULT=YES` does not activate the rendition: the
  receiver never requested `v0_vtt.m3u8` (0 requests in 3 min of playback), and
  `activeTrackIds` stayed empty. Activation needs castbridge (LOAD `activeTrackIds`, or an
  `edit-tracks` control).
- **Gate 2 is not reached yet.** It needs the track active. Open questions remain: the
  missing `X-TIMESTAMP-MAP` and the raw cue text.

### Phase 0, round 2 (2026-10-02, castbridge 0.4.1, "In the Mood for Love")

- **Gate 3 passes with castbridge `textLanguage`.** After the LOAD, `EDIT_TRACKS_INFO
  {language: "it"}` makes the receiver report `activeTrackIds [1]` and fetch `v0_vtt.m3u8`
  and its segments. The viewer saw the subtitles.
- **Pick the right embedded track.** This release's first ita track is `forced`: 561 empty
  segments, only foreign-language lines. The viewer saw nothing until the full ita track
  (subtitle index 1) was mapped. `embedded_pick` must skip `forced` when the user asked
  for subtitles; `forced` is right only as a default with primary-language audio.
- **Gate 2 (sync) is inconclusive, with a lesson.** With the film-time gap playlists for
  media and rendition, the cues showed 1–2 s early (viewer). An objective check measured
  the overlap of the release's cues with speech detected in the produced AAC (1330 s
  window). It peaks at +3.2 s: the cues are early versus the audio in the release itself.
  The peak is shallow (62 % vs 60 % at 0 s; `subalign` refused: `cross_window_disagree`,
  sparse dialogue). A side-loaded control on the same stream could not be read by the
  viewer.
- **Revised assumption: embedded is not "synced by construction".** A muxer can carry a
  mistimed track. Embedded tracks stay first, since they match the exact file, but the
  after-start alignment (ADR 0040 point 2) must also run on them, and `--sub-shift` is
  needed for both.

## Consequences

- castbridge (cast repo) gains text-track activation for in-manifest tracks.
- `live.Job` gains a subtitle map. The serve whitelist adds the rendition names.
- WebVTT cleaning (`srt._vtt_text`) does not touch producer output. Raw cues reach the
  receiver unless nstream rewrites the segments.

## References

ADR 0018, 0020, 0039, 0040. Symbols: `tracks.probe_tracks`, `live.producer_cmd`,
`remux.cast_live`, `subs.auto_subs`, `srt.write_vtt`.
