# 0045. Philips Chromecast: MASTER volume is 0–1; HEVC stutter is delivery, not codec

- **Status:** Proposed
- **Date:** 2026-10-05
- **Deciders:** Phase 0 CLI grid (Odroid, 2026-10-05) confirms the volume contract
  below. Status stays **Proposed** until the OSD photo table and HEVC HEAD/Range
  traces land — Docs can stamp Accepted then. No remux / OSD-scale product default
  change.

## Context

Field, Odroid N2 + Philips 43PUS9235/12 (TPM191E, `192.168.1.228`), nstream 1.43.0,
`cast_mode=dmr`, `cast_remux=true`, catt 0.13.3. **castbridge / mirror / `cast_sender`
absent.** Config sets `cast_receiver_app_id=07841171`. Board traces 2026-10-05.

Two reports, neither a LOAD_FAILED:

1. **Volume.** `catt volume 14` → catt status 14, `volume_control_type: master`. Earlier
   user OSD ~8 at that CLI value (OSD **not** re-measured on this capture; Phase 0
   photo still NEED_USER). Headless `--volume` is 0–100; JSON `volume` is the Cast
   `volume_level` 0–1 (`docs/headless.md`).
2. **1080 HEVC stutter.** Direct debrid HTTP, `delivery: direct`, codec hevc, `ok: true`,
   `volume_zero` at start. No `LOAD_FAILED`. A 1080 remux was `remux_infeasible` (disk ~
   11 GB free, no mirror). A **720 H.264 file remux** via catt LAN HTTP (`video/mp4`,
   `BUFFERED`) was smooth — **runtime triage, not a product answer.**

This is the same TV class as ADR 0005 / 0022 / 0039. Those already showed the DMR
(`CC1AD845`) **plays HEVC Main10 4K HDR10 natively** when the bytes arrive as a complete
Range-served MP4 or as live HLS-TS from the host. A 2026-10-01 live start of *Blade Runner
2049* 3840×1600 HEVC HDR10 on this model was confirmed (ADR 0039 field note).

## Decision

nstream does **not** lower remux defaults to 720p H.264, and does **not** invent an OSD
scale. The volume CLI contract below is the shipped contract (helpers + `--help` /
`--status` / TUI copy). HEVC delivery stays the RCA in Context — no LAN-proxy CODE
until a playback HEAD exists.

1. **Volume CLI contract (shipped).** The Cast protocol has no TV-OSD units.
   `SET_VOLUME` is a float 0–1. `--volume N` / TUI / `catt volume N` are the same
   **Cast percent 0–100**; internally that is `N/100.0`. JSON `volume` is the 0–1
   level; JSON `volume_percent` is the rounded integer percent. On this Philips DMR
   (app `CC1AD845`, cast idle) Phase 0 read `volume_control_type=master` and
   `volume_step_interval=null` at every grid point: the float is the **device master**
   (the TV amplifier), not a second stream attenuator. catt integer % can quantize
   (CLI 14 → nstream `volume` ≈ 0.133 → catt 13). Perceived OSD 12–15 is the
   **acceptance target** on this TV, obtained by setting the Cast percent that the TV
   maps to those ticks — not by remapping `--volume`. There is **no** `osd_max` and
   **no** linear conversion; Cast % and TV OSD are different scales until a photo
   table exists. `--status` reports `volume_control_type` and `volume_step_interval`
   (null when catt omits it).
2. **Receiver app.** `cast_receiver_app_id` is forwarded only on the **castbridge**
   `media-load` (`bridge._media_load_args`, ADR 0013). catt 0.13 has no arbitrary-app-id
   CLI; it launches `CC1AD845`. On this install castbridge is missing, so `07841171` is
   never launched — live session `app_id: CC1AD845`. The catt path emits
   `receiver_app_ignored`. Live HLS already forces the DMR: the custom app `LOAD_FAILED`
   an HLS playlist (ADR 0039 field note).
3. **HEVC delivery.** Keep native HEVC. The stuttering path is **catt handing the TV a
   remote debrid URL** (the TV pulls WAN, contentType is whatever catt guesses —
   `video/mp4` if unset). The working path on this TV is **LAN Range / live HLS with
   video `-c copy`**. Missing `cast_sender` only blocks the **mirror** fallback (ADR 0006 /
   0015), which is 1080p SDR remoting — a last resort, not the HEVC fix. A 720 H.264 remux
   is diagnostic: smaller file + LAN serve, not evidence the SoC cannot decode 1080 HEVC.

## Phase 0 CLI grid (2026-10-05)

Cast idle (player_state UNKNOWN, `content_id` None) on `192.168.1.228`. Volume restored
to ~14 after the grid. OSD photograph: **NEED_USER** (cannot photograph the TV from the
board). Do **not** invent `cast_volume_osd_max` or a linear 0–60 map from the earlier
remembered OSD ~8 pair — CoS already rejected that guess.

| target (CLI %) | nstream `volume` (0–1) | catt % | `volume_control_type` | `volume_step_interval` |
| -------------- | ---------------------- | ------ | --------------------- | ---------------------- |
| 0 | 0.0 | 0 | master | null |
| 14 | ≈ 0.133 | 13 | master | null |
| 25 | 0.25 | 25 | master | null |
| 50 | 0.5 | 50 | master | null |
| 100 | 1.0 | 100 | master | null |

JointSpace on this set (`43PUS9235/12`, api 6.4, `os_type` MSAF_2019_P,
`pairing_type=digest_auth_pairing`, `secured_transport=true`):

- `GET http://TV:1925/system` → 200 (safe fields only).
- `GET http://TV:1925/{1,5,6}/audio/volume` → **404**.
- `GET https://TV:1926/6/audio/volume` → **401**.

Unauthenticated JointSpace volume is **not usable**. DMR/catt volume is the working
plane. Do not ship a paired JointSpace client until product asks for TV-native OSD
sync beyond DMR.

HEVC Accept-Ranges / `Content-Type` HEAD: **NEED_PLAYBACK** (no live debrid URL on
this capture; prior LAN remux endpoint dead). Optional video-copy remux skipped
(disk gate).

## Rationale

| Option | Volume | HEVC | Verdict |
| --- | --- | --- | --- |
| Document CLI ≠ OSD and stop | Leaves MASTER unused | — | Rejected (CoS: not the only fix) |
| Guess `osd_max=60` as default | May hit OSD 12–15 if the guess is right | — | **Rejected** (Phase 0: still no photo table; JointSpace `/audio/volume` is 404/401) |
| Cast 0–1 MASTER, 0–100% CLI, no OSD factor | Honest protocol; UX names the mismatch | — | **Chosen** (this revision) |
| Permanent remux-720 H.264 default | — | Quality downgrade | Rejected (CoS) |
| Mirror / `cast_sender` as the HEVC answer | — | 1080p SDR remoting; binary absent here | Last resort only (ADR 0006) |
| LAN Range / live HLS, video copy | — | Already proven 4K HEVC HDR on this TV | **Chosen** product path |
| LAN Range-proxy of a remote URL (no remux) | — | Would keep HEVC on catt-only hosts | Deferred until playback HEAD |

Cast has no better volume unit than 0–1. Philips JointSpace
(`https://TV:1926/6/audio/volume`) would be the OSD control plane **after pairing**.
Phase 0 closed the unauthenticated GET. Do not pair or POST volume from nstream
until Lab asks.

## Consequences

- `--status` / `caster.status` grow `volume_control_type`, `volume_step_interval`,
  `app_id`, `content_type`, `stream_type` (absent → `null`) and `volume_percent`
  (rounded 0–100, or `null`). Never `content_id` (may be a debrid URL).
- `caster.clamp_volume_percent` / `volume_percent_to_level` / `volume_level_to_percent`
  / `format_volume_bits` are the 0–100% ↔ 0–1 contract. Standalone `--json --volume N`
  emits both `volume` (0–1) and `volume_percent`.
- `--help`, TUI cast menu, and status copy say Cast % ≠ TV OSD, and name MASTER +
  `step=null` when the receiver reports that (this Philips DMR).
- catt-only casts with a configured custom id emit `receiver_app_ignored`. Config docs
  stop implying catt launches that id.
- No ranking / remux-resolution / `cast_mode` default change.
- **Still not shipped:** `cast_volume_osd_max`, JointSpace client, LAN URL-proxy for
  Tier-1. Those wait on the residuals below.

## Residuals still open (Odroid; no install)

Volume:

1. **Done.** CLI grid 0 / 14 / 25 / 50 / 100 — `volume_level`, `volume_control_type=master`,
   `volume_step_interval=null`, catt integer quantization 14→13.
2. **NEED_USER.** Photograph or read the TV OSD at each of those five points.
3. **Done (blocked).** Unauthenticated JointSpace `/audio/volume` is 404 (http :1925) /
   401 (https :1926). Pairing not pursued.

HEVC (do **not** remux to 720; leave the triage file alone):

1. During a **new** 1080 HEVC direct: `catt info -j` (`content_type`, `stream_type`,
   `app_id`, player_state) + the headless JSON (`delivery`, `codec`, `container` if
   present). No stream URL. **NEED_PLAYBACK.**
2. `ffprobe` of the **same** resolved file (via nstream's local/urlproxy path):
   `format_name`, video `codec_name`/`profile`/`pix_fmt`/`bit_rate`, first audio codec.
   Confirm MP4 vs Matroska. **NEED_PLAYBACK.**
3. One HEAD/GET of the debrid URL from the **board** (not the TV): status, `Accept-Ranges`,
   `Content-Type`, `Content-Length`. Redact the URL. **NEED_PLAYBACK.**
4. If disk allows a **video-copy** 1080 HEVC remux to a complete MP4 (no scale): does
   LAN-served HEVC play smooth? That splits “WAN pull” from “decoder”.
5. Whether `castbridge` can be placed on PATH later (CoS names tip first). Live HLS is
   castbridge-only (`remux.cast_live`).

## References

- Board traces 2026-10-05 (odroidn2) + Phase 0 CLI grid 2026-10-05. Symbols:
  `caster.clamp_volume_percent`, `caster.volume_percent_to_level`,
  `caster.volume_level_to_percent`, `caster.format_volume_bits`, `caster.set_volume`,
  `caster.status`, `caster._cast_via_catt`, `bridge._media_load_args`,
  `remux.cast_live`, `cast_flow._with_container_mime`, `quality.CAST_VIDEO_DECODABLE`.
- ADR 0005, 0007, 0013, 0015, 0022, 0039. catt 0.13 `DefaultCastController.play_media_url`
  (contentType defaults to `video/mp4`). Google Cast `Volume` / `controlType`.
- Philips JointSpace v6 (`/audio/volume` `{current,min,max}`) — unauthenticated GET
  is 404/401 on this TPM191E; pairing required.
