# Receiver matrix — Philips 43PUS9235/12 (Chromecast built-in), 2026-10-01

Sender: `catt cast <url>` (Default Media Receiver). Host: sender, LAN 192.0.2.1; TV
192.0.2.10. Media served by a Range+CORS static server on port 45901. Clips: `testsrc2`
1280x720@25 + a sine tone; audio verdict by ear (the maintainer at the TV), with an AAC control
clip to rule out volume. Receiver volume 0.35 for every audible test.

| # | Container / delivery | Video | Audio | Receiver state | Picture | Sound |
|---|---|---|---|---|---|---|
| 1 | MP4 progressive (faststart) | H.264 | AC-3 192k | PLAYING | yes | **no** |
| 2 | MP4 progressive | H.264 | AAC 192k (control) | PLAYING | yes | yes |
| 3 | MP4 progressive | H.264 | E-AC-3 192k | PLAYING | yes | **no** |
| 4 | HLS VOD, MPEG-TS segments (complete playlist) | H.264 copy | AAC | PLAYING | yes | yes |
| 5 | HLS EVENT, MPEG-TS, playlist growing (`ffmpeg -re`) | H.264 copy | AAC | PLAYING (duration -1) | yes | yes |
| 6 | HLS EVENT, MPEG-TS, playlist growing | HEVC 8-bit copy (`hvc1`) | AAC | PLAYING (duration -1) | yes | yes |

Notes:
- `active_tracks` was `[]` in every case, audible or not: it is not an audio-health signal.
- Cast URLs must be cache-busted (`?v=N`): the receiver reused a cached 40 s file for the same URL.
- Not covered: seeking within a growing playlist, 4K, HEVC Main10/HDR10/Dolby Vision, a real
  remote (debrid) source as ffmpeg input, the castbridge path, side-loaded subtitles.

## Round 2 — acceptance gates, real source (2026-10-01, evening)

Source: real debrid link (Torrentio/Real-Debrid), *The Great Gatsby* 2013 1080p BluRay, MKV,
HEVC **Main 10, HDR10 (PQ/BT.2020) + Dolby Vision profile 8 RPU** (bl compat 1, no EL),
DTS-HD MA 5.1. ffmpeg 9.0.2 reading the debrid URL directly (`-rw_timeout 30000000`).
Producer: `-c:v copy -c:a aac -f hls -hls_time 4 -hls_segment_type mpegts`.

| # | Variant | Sender | Result |
|---|---|---|---|
| 7 | EVENT, AAC **5.1 448k**, `-ss` before `-i` | catt | joined at live edge (seg 12), stalled BUFFERING, never fetched more |
| 8 | same, playlist complete (ENDLIST) | catt | fetched seg 0–1, then UNKNOWN → **content issue, not playlist growth** |
| 9 | synthetic HEVC Main10 HDR10 (no DV), AAC stereo | catt | plays, picture + sound |
| 10 | real source, DV RPU **stripped** (`dovi_rpu=strip=1`), AAC 5.1 | catt | UNKNOWN → DV is not the cause |
| 11 | real source, DV kept, AAC **stereo 192k** | catt | **plays**, but A/V out of sync |
| 12 | #11 + `-noaccurate_seek` (audio starts on the video keyframe) | catt | **plays, in sync** (stream start_time v 1.483 / a 1.469) |
| 13 | #12 as growing EVENT playlist | catt | plays, but **joins at the live edge** (skips ~2 min), `duration -1`, `catt seek` → "Stream is not seekable" |
| 14 | #13 + `#EXT-X-START:TIME-OFFSET=0,PRECISE=YES` | catt | ignored: still live edge |
| 15 | #13 | **castbridge** (`streamType: BUFFERED`) | **starts at segment 0**, seek +90 s via `media-control` works |
| 16 | #15 + side-loaded WebVTT (`subtitle_url`) | castbridge | caption track active (`active_tracks [1]`), cues render; offset from the manual −4800 s shift, not from delivery |

Findings:
- AAC 5.1 in HLS-TS stalls this receiver; **stereo AAC works**. (Surround loss vs today's
  full-file remux, which keeps channel-aware AAC.)
- `-noaccurate_seek` is required with `-ss` + video copy, or audio leads the video.
- Dolby Vision profile 8 with HDR10 base layer is fine copied as-is.
- The sender must LOAD as **BUFFERED** (castbridge does; catt's LIVE detection starts at the
  live edge and forbids seek). `EXT-X-START` is ignored by the DMR.
- Seek inside the produced range works via castbridge; a seek beyond it still needs the
  restart-ffmpeg design.
- ffmpeg produced the remaining 71 min (1.5 GB of segments) in seconds from the debrid link:
  the "segment window" is not small unless the producer is paced or old segments are pruned.

Gate 3 (full runtime, no stall) — **passed**: #15/#16 from 1h20m to the end of the film,
3727 s of produced content (792 segments), polled every 60 s: PLAYING throughout, 0 stalls,
the receiver ended by itself at `ENDLIST` (20:27–21:28).

Open: 4K source not yet tested (1080p HDR10 only).
