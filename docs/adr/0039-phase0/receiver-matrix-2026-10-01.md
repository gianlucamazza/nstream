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
