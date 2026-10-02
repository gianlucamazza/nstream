# 0044. Live `started` is an observed player state; dual-layer DV is not live-HLS-native

- **Status:** Accepted
- **Date:** 2026-10-02
- **Deciders:** maintainer
- **Amends:** 0031 point 3 (live/castbridge path only); 0039 (live eligibility and the
  complete-file fallback)

## Context

Field incident, 2026-10-02, Philips 43PUS9235/12, *After Hours* (1985) Criterion 1080p
DV HDR10 H.265 EAC3 ITA, `nstream --json --cast --quality 1080 --audio-lang ita`:

- `remux.cast_live` served a growing HLS-TS playlist (`-c:v copy -c:a aac`).
- The TV fetched `hls/index.m3u8`.
- Headless emitted `{"ok": true, "delivery": "live", "audio_verified": true}`.
- Immediately afterwards `caster.status` / `catt info` reported `player_state: UNKNOWN`,
  `receiver_error: ERROR`, `content_id: None`.
- The complete-file remux (ADR 0039 fallback) never ran.

Two facts, same incident.

**1. Fire-and-return invents `started`.** `bridge.cast_load` already treats
`PLAYING` / `PAUSED` / `BUFFERING` as started. When `follow=False` and the daemon socket
ends after a `media-load` ack (`load_ok`) with **no** media-status, it still yields
`started` (`bridge.cast_load`, the `elif not follow and load_ok` branch). ADR 0031 point 3
chose that bar on purpose: “handoff accepted, not playback observed”, so a fire-and-return
would not become a false negative. On the live HLS path it is too low. The Default Media
Receiver can ack the LOAD, GET the playlist, and refuse the media — the same signature as
ADR 0022 (mkv LOAD: `UNKNOWN` + `receiver_error: ERROR` + `content_id: None`).
`remux.cast_live` then keeps `CastResult(started=True)` and `cast_flow` does not fall
through to `remux.cast_file`.

**2. The live copy was not DMR-sane.** The first segment (`index0.ts`) probed as
`hevc / yuv420p10le` with AAC `channels=0` / `sample_rate=0`. `quality.unsupported_reason`
excludes only Dolby Vision **Profile 5**; a dual-layer source (`tracks.Tracks.n_video >= 2`,
Profile 7 EL) is already mapped to `0:v:0` on the complete-file path (`remux.remux_to_file`)
and copied as-is on the live path (`live.Producer`, `-c:v copy` into MPEG-TS). ADR 0039's
matrix already showed Profile 8 single-layer (Gatsby 1080p Main10 HDR10 + DV RPU, no EL)
and 4K Main10 HDR10 without DV (Blade Runner 2049) **play** live. A blanket “DV never
live” would regress those. The miss is dual-layer / a playlist head the DMR cannot decode.

## Decision

1. **On the castbridge path, `started` is an observed player state.**
   `bridge.cast_load` yields `started` only for `PLAYING` / `PAUSED` / `BUFFERING` inside
   `_LOAD_TIMEOUT`. EOF or the deadline with only a `media-load` ack is
   `failed` / `cast_startup_failed`. `receiver_error` before `started` stays
   `failed` / not-started (already true). `remux.cast_live` already teardowns and returns
   `None` when `not out.started`, so the complete-file remux takes over.

   Catt / complete-file remux keep ADR 0031 point 3: `started=True` means the handoff was
   accepted (catt rc 0). That clause is amended only for castbridge.

2. **Live is not attempted for a dual-layer Dolby Vision source.**
   `remux.live_feasible` and `remux.cast_live` return false / `None` when
   `_probe_meta(url).n_video >= 2` (the same signal `remux.remux_to_file` already uses to
   drop the enhancement layer). Profile 8 single-layer (`n_video == 1`) stays live.
   Profile 5 stays excluded at ranking.

3. **The live LOAD waits on a DMR-sane first segment.** After `_await_live` and before
   `cast_delivery.drive_bridge`, `remux._live_head_ok` ffprobes the first listed `.ts`
   (local path, never a debrid URL). It must show a video codec in
   `quality.CAST_VIDEO_DECODABLE` and an audio stream with `channels > 0`. Otherwise
   teardown and `return None` → complete-file remux. No LOAD on a rotten playlist head.

## Rationale

| Option | Verdict |
| ------ | ------- |
| Keep `load_ok` → `started` (ADR 0031 §3 as written) | Rejected for live: tonight's `ok: true` + dead TV |
| Poll `catt info` after return | Rejected in 0031 (race); still a race |
| Require `PLAYING`, not `BUFFERING` | Rejected: HLS live sits in BUFFERING for seconds; every fire-and-return would wait on first frame |
| Ban every `StreamInfo.dv` from live | Rejected: 0039 matrix #11/#15 (P8, no EL) and BR2049 Main10 already play |
| Demote all DV in ranking | Rejected: After Hours had no 1080p Italian non-DV sibling; the same release's file remux is the fallback |
| Observed state + skip EL + first-segment probe | **Chosen.** Restores the 0039 fallback, keeps P8 live, refuses a LOAD the DMR will ERROR |

Accepted gap: `BUFFERING` then `receiver_error` *after* fire-and-return has already
returned. Tonight never left `UNKNOWN`. A settle window is a follow-up, not this ADR.

## Consequences

- Headless live `ok: true` requires the receiver to have entered `PLAYING` / `PAUSED` /
  `BUFFERING`. A playlist GET is not enough.
- Dual-layer DV titles pay the complete-file prepare (video copy of `0:v:0`, audio AAC)
  instead of a live start that the DMR refuses.
- Fire-and-return live casts wait up to `_LOAD_TIMEOUT` (45 s) for a media-status instead
  of returning on the LOAD ack. Direct / file casts unchanged.
- Tests that photographed `load_ok` + EOF → `started` (`test_cast_load_eof_before_any_state_no_follow_yields_started`) invert: that path is now `cast_startup_failed`.
- Field gate before **Accepted**: recast *After Hours* 1985 `--quality 1080 --audio-lang ita`.
  Expect `delivery: file` (or live that reaches `PLAYING`), never `ok: true` + `receiver_error`.

## Post-scriptum (2026-10-02)

Field gate passed, same TV. Live started converting, `_live_head_ok` refused the first
segment (`live: primo segmento non riproducibile dal DMR`), complete-file remux of the
same 2.04 GB ITA release, `delivery: file`, receiver `CA5T0001`. `--status`: `PLAYING`,
position advancing, `receiver_error: null`. The dual-layer (`n_video >= 2`) skip did not
fire on this title; the playlist-head probe did.

## References

- ADR 0031 (`cast_delivery.CastResult.started`, `bridge.cast_load`)
- ADR 0039 (`remux.cast_live`, `remux.cast_file` fallback, `docs/adr/0039-phase0/receiver-matrix-2026-10-01.md`)
- ADR 0022 (same DMR refuse signature on a bad container)
- ADR 0017 (`quality.CAST_VIDEO_DECODABLE`, `tracks.Tracks.n_video`)
- `bridge.cast_load`, `remux.cast_live`, `remux.live_feasible`, `remux._live_head_ok`,
  `remux.remux_to_file` (EL drop)
