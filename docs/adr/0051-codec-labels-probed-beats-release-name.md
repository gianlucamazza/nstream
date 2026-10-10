# 0051. Codec labels: probed beats release name

- **Status:** Accepted
- **Date:** 2026-10-10
- **Deciders:** field report (the board, Deadpool @ 12810a18) + ADR 0017 / 0045 follow-up

## Context

`quality._parse_codec` reads the release name. A 1080 row nstream labelled
`x265 HEVC` (Deadpool) was **H.264** per ffprobe of the same file. Ranking still
needs the cheap name parse (no probe of every catalog row). After pick, the LAN
proxy / remux settle / `cast_vet.vet_cast_video` paths already ffprobe that url
(`tracks.probe_tracks`, memoized). Those paths still preferred a caller-supplied
claimed codec (`video_codec or tr.video_codec`) for `urlproxy.plan` /
`CAST_VIDEO_DECODABLE` / `hev1` rewrap, and `--json` / the stream label always
emitted the name parse.

An extra ffprobe on the picker or `--explain` ranking would be a new network
cost. ADR 0017 already paid the probe for cast vetting; reporting must not pay
another.

## Decision

1. **Routing.** Where a probe already ran, `tr.video_codec` / `tr.codec_tag` win
   for `urlproxy.plan` (`hev1` → rewrap, `CAST_VIDEO_DECODABLE` reason) and the
   remux `-tag:v hvc1` decision. The claimed name is only the fallback when the
   probe is empty. No remux-720 default.
2. **Display / `--json`.** `tracks.honest_codec(url, claimed)` is cache-only.
   A hit with a real `video_codec` returns `(codec, "probed")`; otherwise
   `(claimed, "release_name")`. `headless_play.describe_stream` and
   `explain._row_data` add `codec_source` (additive). The TUI stream label
   suffixes `(claimed)` when the source is the release name.
3. **No new probe** on the hot path. Picker / `--explain` / pre-play JSON stay
   claimed unless some earlier call already memoized that url.

## Rationale

| Option | Routing | Label | Verdict |
| --- | --- | --- | --- |
| Keep name parse everywhere | hev1/hvc1 can fire on a lying x265 tag | Deadpool shows HEVC | Rejected (field) |
| Probe every catalog row | Honest | Honest | Rejected (budget) |
| Probed when already paid; else mark claimed | Honest on LAN/remux/vet | Honest + additive JSON | **Chosen** |

## Consequences

- `--json` `stream.codec_source` is `probed` \| `release_name`. Existing
  `stream.codec` may change after a probe (H.264 instead of hevc) — that is the
  fix; the field stays a string. Scripts that assumed the name parse should
  read `codec_source`.
- `quality.StreamInfo.codec` stays the name parse (ranking / ADR 0026).
- Residual: if ffprobe fails (`Tracks.video_codec` empty) but remux still
  runs, ffmpeg may write HEVC as `hev1` rather than `-tag:v hvc1`. Safe
  direction — we do not invent a tag from the release name.
- Debrid URLs stay out of argv, logs, and `--json`.

## References

- `tracks.honest_codec`, `tracks.cached_tracks`, `headless_play.describe_stream`,
  `caster.lan_media`, `urlproxy.plan`, `remux.remux_to_file`, `labels.stream_label`
- ADR 0017 (real codec before cast), ADR 0026 (name parse), ADR 0045 (LAN plan /
  hev1 rewrap). Field: the board @ 12810a18, Deadpool 1080 labelled x265, ffprobe
  H.264.
