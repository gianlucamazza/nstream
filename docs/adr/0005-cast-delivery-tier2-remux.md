# 0005. Cast delivery: Tier-2 on-host audio remux to a complete file

- **Status:** Accepted
- **Date:** 2026-06-04
- **Deciders:** project maintainer
- **Implemented in:** `src/nstream/remux.py` (+ wiring in `quality.py`/`config.py`/`cli.py`)

> Retrospective: this ADR formalises a delivery architecture that was decided interactively
> (with the maintainer) and validated end-to-end during one session. It records the empirical
> findings so the choice — and the alternatives ruled out — aren't re-litigated.

## Context

Casting a movie to the target TV (Philips 43PUS9235, Android TV with Chromecast built-in) must
reach **native fidelity** (HEVC 10-bit, 4K, HDR10) by a **free-software, standard-protocol** path
— no third-party player app, no Google receiver registration. Empirically verified against the
device:

- The **Default Media Receiver** (DMR) plays **HEVC Main10 4K HDR10 natively** (`PLAYING`
  confirmed) but **does not pass through Dolby audio**: an AC-3 clip plays silently, an AAC clip is
  audible. Per Google Cast docs, AC-3/E-AC-3/Atmos passthrough is a *Web Receiver SDK* feature, not
  the bare DMR.
- This makes Dolby/DTS titles (`quality._CAST_NEEDS_REMUX` = ac3/eac3/dts/dtshd/truehd) silent on a
  direct cast — previously they were *excluded* from casting (`quality.unsupported_reason`).

## Decision

For a cast whose audio the DMR can't decode, **remux on the host** — keep the original video
(`-c copy`, so HEVC/4K/HDR are preserved), transcode only the audio to AAC, write a **complete
temp MP4**, and let **catt's own HTTP server serve it** (`remux.cast_file` spawns a detached
`catt cast <file>`). Selection **prefers a native-AAC release** (`quality.score_components`,
`cast=True`) so this Tier-2 path only triggers when no AAC alternative exists, and a
**remux-only resolution cap** (`cast_remux_max_resolution`, default 1080p) avoids fetching a full
4K just to cast. The H.264 1080p mirror (`skill-cast`/openscreen) remains the last resort.

## Rationale

The DMR **only plays a complete, `Content-Length`'d, Range-served file** — every
streaming-while-transcoding delivery was tested and shows a black screen (the receiver does one
GET without a `Range` header and gives up). So the file must be remuxed in full before casting,
and catt's server (which elicits the `206`/Range exchange the DMR wants) is the delivery.

| Alternative | Verdict |
|-------------|---------|
| **DMR + audio remux → complete file (chosen)** | Works. Native video; audio → AAC (≤5.1). Cost: a prepare wait (whole-file fetch). |
| Cast **remoting** (openscreen sender) | Dead end: `cast_sender --probe-caps` shows the receiver advertises only `video=[h264 vp8 vp9] audio=[baseline-set aac opus]` — **no HEVC/4k/Dolby** over remoting; openscreen's standalone remoting is handshake-only (re-encodes). |
| **Custom CAF receiver** | Non-libre: Google dev account ($5) + per-device registration + hosted app. |
| **Streaming** the remux (on-the-fly fMP4 200, `--stream-type live`, HLS-fMP4, growing-file + Range/Content-Length) | All **black** / no segments on this DMR — it requires a complete file. |
| **Native player** on the TV (Kodi/VLC via ADB/JointSpace) | Vendor-locked and/or a third-party app — rejected as the default for a libre project. |
| **Embedded audio-track selection** (`activeTrackIds` on a direct cast) | Not possible on the DMR: [Google Cast docs](https://developers.google.com/cast/docs/android_sender/media_tracks) — *"the Default Media Receiver allows you to use only the **text** tracks … to work with the audio and video tracks, you must develop a Custom Receiver."* So picking a non-default audio dub without a registered receiver **requires the remux** (keep one track). catt/pychromecast expose no audio-track API either. |

## Consequences

- **Gain:** Dolby/DTS titles now cast at full **native video** fidelity (4K/HDR/HEVC) with audible
  audio, all via the standard Cast media protocol — no third-party app, no registration.
- **Cost — prepare wait:** the whole file is downloaded + remuxed before playback (this DMR can't
  stream a transcode). Mitigated by preferring AAC (remux is rare) and the 1080p remux cap. Direct
  (AAC) casts are unaffected and stream 4K for free.
- **Cost — audio:** Atmos objects and DTS/TrueHD *lossless* are not preserved (audio → AAC ≤5.1);
  this is a device limit (no libre passthrough path), not a project choice.
- **New module + lifecycle:** `remux.py` owns a temp file on `$XDG_CACHE_HOME/nstream/remux/`
  (disk, never `$XDG_RUNTIME_DIR` tmpfs) and a detached serving `catt`, tracked in a state file so
  `--stop` (and the next run's GC) tear them down. `quality`/`config`/`cli` gain `cast_remux`,
  `cast_audio_codec`, `cast_remux_max_resolution`.
- **Per-language audio (1.11/1.12):** the remux keeps the audio track matching the user's language
  priority (`cfg.primary`/`audio_langs`), by its **absolute ffprobe stream index** — a dual/multi-audio
  release no longer casts the wrong dub. This is now the cast's language mechanism, not just a Dolby
  fix: **Google Cast docs confirm the Default Media Receiver exposes only *text* tracks to the Track
  API — audio track selection requires a custom (registered, non-libre) receiver**, so on a libre path
  the only way to pick an embedded audio track is to remux the file down to that single track. The
  decision lives in `stream_select.vet_cast_audio` (direct / remux / reselect / fallback-subs); see
  CLAUDE.md → Casting. Bitrate scales with the channel count (stereo 192k → 5.1 448k → 7.1 640k); an
  already-decodable track is stream-copied (no re-encode). A Dolby-Vision *dual-layer* (profile 7)
  source is flagged: only `0:v:0` is mapped, so its enhancement layer drops to HDR10 base.
- **Guards (1.11):** a single probe per cast feeds the decision (a positively-AAC release name skips
  it entirely → instant Tier-1). `cast_remux_max_size_gb` (default 20) is the download budget: in
  ranking, `quality.remux_within_size` demotes a likely-remux release over budget below any feasible
  alternative (size is the real cost — an *unlabelled* 4K REMUX reads as decodable/unknown audio yet
  carries the lossless disc track, so the resolution cap alone can't catch it; a field cast of a 3 hr
  REMUX surfaced an 86 GB pick that the cap let through). Before the remux itself: a free-disk
  pre-check aborts to a direct cast if it won't fit, and the same budget prompts for confirmation on
  an interactive tty before a large download. Headless callers proceed without blocking.

## References

- `src/nstream/remux.py`, `src/nstream/quality.py` (`_CAST_NEEDS_REMUX`,
  `score_components(cast=True)`), `src/nstream/config.py` (`cast_remux*`).
- Protocol investigation + the rejected remoting path:
  `~/Workspace/tooling/cast/native/docs/remoting-design.md`.
- Google Cast supported media: <https://developers.google.com/cast/docs/media>.
