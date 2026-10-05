# 0045. Philips Chromecast: MASTER volume is 0–1; HEVC stutter is delivery, not codec

- **Status:** Proposed
- **Date:** 2026-10-05
- **Deciders:** pending field gate (Lab → CoS). Investigation only — no product default change.

## Context

Field, Odroid N2 + Philips 43PUS9235/12 (TPM191E, `192.168.1.228`), nstream 1.43.0,
`cast_mode=dmr`, `cast_remux=true`, catt 0.13.3. **castbridge / mirror / `cast_sender`
absent.** Config sets `cast_receiver_app_id=07841171`. Board traces 2026-10-05.

Two reports, neither a LOAD_FAILED:

1. **Volume.** `catt volume 14` → catt status 14, `volume_control_type: master`. Earlier
   user OSD ~8 at that CLI value (OSD **not** re-measured on this capture). Headless
   `--volume` is 0–100; JSON `volume` is the Cast `volume_level` 0–1
   (`docs/headless.md`).
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
scale. It tells the truth about the two control planes it already speaks, and it surfaces
the Cast session fields the next board capture needs.

1. **Volume.** The Cast protocol has no TV-OSD units. `SET_VOLUME` is a float 0–1.
   `volume_control_type: master` means that float is the **device master** (what the TV
   amplifier uses), not a second stream attenuator. catt maps `catt volume N` → `N/100.0`.
   nstream `--volume N` is that same 0–100 Cast percent. Perceived OSD 12–15 is the
   **acceptance target** on this TV, obtained by setting the Cast level that the TV maps
   to those ticks — not by lying about `--volume`. A linear Philips 0–60 OSD would explain
   the one existing pair (`14/100 × 60 ≈ 8.4`); that max is a **hypothesis** until an OSD
   table or JointSpace `max` is read. `--status` now reports `volume_control_type` (and
   `volume_step_interval` when catt sends it) so the next capture does not drop the
   control plane.
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

## Rationale

| Option | Volume | HEVC | Verdict |
| --- | --- | --- | --- |
| Document CLI ≠ OSD and stop | Leaves MASTER unused | — | Rejected (CoS: not the only fix) |
| Guess `osd_max=60` as default | May hit OSD 12–15 if the guess is right | — | Rejected until an OSD table or JointSpace `max` exists |
| Cast 0–1 MASTER, opt-in OSD max later | Honest protocol; UX target after one measurement | — | **Chosen** |
| Permanent remux-720 H.264 default | — | Quality downgrade | Rejected (CoS) |
| Mirror / `cast_sender` as the HEVC answer | — | 1080p SDR remoting; binary absent here | Last resort only (ADR 0006) |
| LAN Range / live HLS, video copy | — | Already proven 4K HEVC HDR on this TV | **Chosen** product path |
| LAN Range-proxy of a remote URL (no remux) | — | Would keep HEVC on catt-only hosts | Next, after contentType + Range traces |

Cast has no better volume unit than 0–1. Philips JointSpace
(`https://TV:1926/6/audio/volume`, pairing on Android) can report `{current, min, max}`
OSD ticks — that is the OSD control plane if the board answers. UPnP RenderingControl is
the same idea. Neither is wired today; do not pair or POST volume from nstream until the
GET is captured.

## Consequences

- `--status` / `caster.status` grow `volume_control_type`, `volume_step_interval`,
  `app_id`, `content_type`, `stream_type` (absent → `null`). Never `content_id` (may be a
  debrid URL).
- catt-only casts with a configured custom id emit `receiver_app_ignored`. Config docs
  stop implying catt launches that id.
- No ranking / remux-resolution / `cast_mode` default change.
- **Not shipped here:** `cast_volume_osd_max`, JointSpace client, LAN URL-proxy for
  Tier-1. Those wait on the traces below.

## Traces still needed (Odroid; no install)

Volume (read OSD from the TV, not from memory):

1. `catt -d 192.168.1.228 info -j` at CLI volume **0, 14, 25, 50, 100** — keep
   `volume_level`, `volume_control_type`, `volume_step_interval` / `stepInterval`.
2. Photograph or read the TV OSD at each of those five points.
3. Unauthenticated probes only (redact any pairing dialog; do not complete pairing
   unless Lab asks):
   `curl -sk https://192.168.1.228:1926/6/audio/volume`
   `curl -s http://192.168.1.228:1925/1/audio/volume`
   `curl -sk https://192.168.1.228:1926/6/system`

HEVC (do **not** remux to 720; leave the triage file alone):

1. During a **new** 1080 HEVC direct: `catt info -j` (`content_type`, `stream_type`,
   `app_id`, player_state) + the headless JSON (`delivery`, `codec`, `container` if
   present). No stream URL.
2. `ffprobe` of the **same** resolved file (via nstream's local/urlproxy path):
   `format_name`, video `codec_name`/`profile`/`pix_fmt`/`bit_rate`, first audio codec.
   Confirm MP4 vs Matroska.
3. One HEAD/GET of the debrid URL from the **board** (not the TV): status, `Accept-Ranges`,
   `Content-Type`, `Content-Length`. Redact the URL.
4. If disk allows a **video-copy** 1080 HEVC remux to a complete MP4 (no scale): does
   LAN-served HEVC play smooth? That splits “WAN pull” from “decoder”.
5. Whether `castbridge` can be placed on PATH later (CoS names tip first). Live HLS is
   castbridge-only (`remux.cast_live`).

## References

- Board traces 2026-10-05 (odroidn2). Symbols: `caster.set_volume`, `caster.status`,
  `caster._cast_via_catt`, `bridge._media_load_args`, `remux.cast_live`,
  `cast_flow._with_container_mime`, `quality.CAST_VIDEO_DECODABLE`.
- ADR 0005, 0007, 0013, 0015, 0022, 0039. catt 0.13 `DefaultCastController.play_media_url`
  (contentType defaults to `video/mp4`). Google Cast `Volume` / `controlType`.
- Philips JointSpace v6 (`/audio/volume` `{current,min,max}`) — unconfirmed on TPM191E.
