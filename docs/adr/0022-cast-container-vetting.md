# 0022. Vet the cast CONTAINER before casting

- **Status:** Accepted
- **Date:** 2026-07-22
- **Deciders:** project maintainer

## Context

nstream models the Chromecast Default Media Receiver's (DMR) **codec** support on two axes —
audio (ADR 0005: AC-3/E-AC-3/DTS/TrueHD are not decoded → Tier-2 remux) and video (ADR 0017:
the real ffprobe codec must be in {h264, hevc, vp8, vp9}) — but it has **no concept of the
container**. `StreamInfo` carried resolution/codec/audio/hdr but no container; `parse_stream`
reads only the name+title text (`quality._text`), never `behaviorHints.filename` where the
extension lives; and a direct-cast LOAD sends an empty `contentType`, so the DMR sniffs.

Field incident (Independence Day: Resurgence, Philips 43PUS9235, app CC1AD845, 2026-07-22): a
direct cast of every `.mkv` release — HEVC video, AAC audio, both inside the DMR's decoder set —
was **refused at LOAD**: `player_state UNKNOWN`, `receiver_error ERROR`, `content_id None`
(the TV stuck on the cast icon). The receiver even fetched the side-loaded `subs.vtt` from
nstream's own server, so it connected and then rejected the media. Casting the SAME movie as an
`.mp4` (verified via `catt` + ffprobe) played immediately (`PLAYING`, position advancing). The
container, not the codec, is unsupported: the DMR loads MP4/WebM/CMAF, not Matroska/AVI. 28 of
the top 40 ranked picks for the title were `.mkv`, ranked first — so the common case failed.

This is distinct from ADR 0017's "PLAYING + black": here the receiver DOES signal an error, but
it is a LOAD refusal at container-sniff, before decode. Selection time is still the fix.

## Decision

Model the container as the missing sibling of the codec model, at the same three layers.

1. **Name-parse** (`quality.StreamInfo.container`, `quality._parse_container`): the canonical
   container from `behaviorHints.filename` (fallback: the url path tail). The first `quality`
   code to read `behaviorHints` — deliberate, the extension lives only there. The parse cache
   key gains the filename so two releases differing only by container don't collide.
2. **Probe-verify** (`tracks.Tracks.container`): the raw ffprobe `format_name` from the SAME
   memoized probe the audio/video vetting already runs (zero extra network). It confirms or
   overrides the extension toward INCOMPATIBLE, so a `.mp4` that is really Matroska is rewrapped,
   not cast black.
3. **Decodable set + vet** (`quality.CAST_CONTAINER_DECODABLE = {mp4, webm}`,
   `stream_select.vet_cast_container`, in the ADR-0017 mold): if the chosen container is
   castable, cast directly; otherwise **reselect** a DMR-compatible-container candidate that is a
   **verified direct cast in the target language** (its real first audio track, not the release
   name, carries the dub) — a free direct cast, the biggest win especially at 4K; otherwise route
   the pick through the **Tier-2 rewrap to MP4**. The language must be _verified_: a name-`multi`
   MP4 whose real first track is another dub must not preempt the target-language rewrap (an
   Italian request landing on a Spanish MP4 was the concrete failure). When the rewrap itself is
   unavailable (`cfg.cast_remux` off or ffmpeg absent) and the mirror is available, **mirror**
   (mpv decodes any container) rather than hand the DMR the .mkv it will refuse. The rewrap already exists:
   `remux.remux_to_file` emits `.mp4` and does `-c:v copy -c:a copy` when the audio is decodable,
   so a decodable-audio mkv→mp4 is a pure container rewrap (no `remux.py` change). A settled-stream
   recheck folds `needs_rewrap` into the remux gate so the guarantee holds whichever release the
   audio reselect landed on. Unknown ("") keeps the benefit of the doubt.
4. **webm-vs-mkv**: ffprobe reports `matroska,webm` for both, so the filename **extension** is
   authoritative for that split; the probe is authoritative for mp4-vs-matroska.
5. **contentType on the direct LOAD** (`quality.container_mime`): declare `video/mp4` /
   `video/webm` instead of leaving the DMR to sniff (orthogonal correctness; setting it on an
   mkv would NOT fix the refusal — the vet, not the contentType, is load-bearing).
6. **Ranking demotion**: a DMR-incompatible container counts in `quality._likely_needs_remux`,
   so a 4K mkv is demoted below an mp4 alternative and the `cast_remux_max_resolution` /
   `cast_remux_max_size_gb` caps apply — the common path stays a direct mp4 cast, the rewrap stays
   rare (the container twin of "selection prefers AAC").

## Consequences

- An mkv cast becomes: an mp4 reselect (free), an mp4 rewrap (a capped, mirror-guarded download),
  the mirror, or an explicit error — never a stuck LOAD. A 4K mkv with no mp4 twin is a
  pathological rewrap, so ADR 0015 auto-prefers the mirror (see ADR 0023 for the HDR fix that
  makes that mirror watchable).
- No extra probe on the happy path (memoized with the audio/video vetting); the reselect probes
  at most `probe_cap` (4) candidates.
- A 1080p mkv now pays a cheap copy/copy rewrap (a prepare-wait download) where it previously
  failed silently.
- With `cfg.cast_remux` disabled or ffmpeg absent, a bad-container pick **mirrors** (when the
  mirror is available) instead of degrading to a direct cast the DMR refuses — the fallback chain
  is reselect → rewrap → mirror, never a silent black cast. Only when the mirror is _also_
  unavailable does it degrade to the original direct-cast failure (no backend can deliver an
  MP4 or decode locally).
