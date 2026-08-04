# 0028. Vet the REAL duration against the expected runtime

- **Status:** Accepted
- **Date:** 2026-08-04
- **Deciders:** project maintainer

## Context

Field incident (The Punisher S01E01, 2026-08-04):
`nstream --json --local --series --season 1 --episode 1 --audio-lang eng --sub-lang eng
--quality 720 "The Punisher"` returned `ok: true` and opened a **30-second** file in mpv — a
"removed for copyright" placeholder — for an episode of ~55 minutes. The JSON reported
`stream={"resolution":720,"size_gb":3.18,"cached":false,"backend":"p2p"}`,
`audio_verified:false`. Nothing in the run was wrong except the content itself.

Three guards were no-ops at once:

1. **The ADR 0025 size check is HTTP-only by construction.** It lives inside a
   `GET Range: bytes=0-0` and reads `Content-Range` (`net.probe_url`). The pick was a **P2P**
   release (a magnet resolved through `engine.resolve`), so there was no header to compare
   against the announced 3.18 GB. Nothing about the size check is wrong — it simply cannot
   speak for a transport that has no such header.
2. **`_verify_availability` / `_ensure_playable` return early on `playback_backend == "local"`**
   and, in any case, only target candidates that already carry a `url`. A P2P candidate is
   never a probe target.
3. **The `--audio-lang` branch bypasses `prepare_stream` entirely** (`headless_play.auto_play`):
   it calls `pick_audio_stream_verified` and builds the `VettedStream` by hand, so none of
   `prepare_stream`'s guards ran.

The measurement that would have caught it **had already been paid for**. `tracks.probe_tracks`
asks ffprobe for `format=duration` and exposes it as `Tracks.duration`; that probe ran on this
very file (it is what produced `audio_verified: false`). The number was in memory, in the same
object, and nobody compared it to anything — `duration` had exactly two consumers, the remux
progress line and the bench.

This is the third instance of the same shape as ADR 0017 (video codec) and ADR 0022
(container): a fact about the file that only ffprobe knows, cheap because the probe is already
memoized, and load-bearing only at selection time.

## Decision

**Vet the real duration against the title's expected runtime — the backend-agnostic twin of
the size check.** The size proves "the transport is not delivering the announced file"; the
duration proves "this file is not the video", regardless of how it is delivered.

1. **The measurement** (`availability.vet_duration`, `availability.DurationVerdict`). Reads the
   same memoized `tracks.probe_tracks` the audio/cast vetting already runs — zero extra ffprobe
   on every path that vets audio. Rejects when
   `duration < MIN_RUNTIME_RATIO (0.35) × expected`, with a floor of
   `MIN_EXPECTED_S (600 s)` so shorts and clips are never judged.
2. **One-way by design.** Only _too short_ is a verdict. An extended cut, a double episode, or
   a season pack with the wrong `fileIdx` are all _longer_ than expected; rejecting long files
   would break correct playback to catch nothing.
3. **The expected runtime** (`api.expected_runtime_s`). For a series the `runtime` lives on the
   **series** meta and is the length of the _typical episode_ — Cinemeta's `videos` entries
   carry none — so the series id is derived from the episode id (`tt5675620:1:1` →
   `tt5675620`). Reads `meta_cached_disk` (token-free, TTL 600 s, usually already warm from the
   preview pane). **An unknown runtime is never replaced by a default**: inventing "45 min for
   a series" would manufacture false positives on specials and recaps.
4. **Confidence-gated.** Unknown expected runtime, unreadable duration (ffprobe missing,
   failed, or timed out), or a runtime below the floor all **pass**. This matches the stance of
   `_audio_langs_of` and `_cast_plan_for`: a failed probe never blocks playback. The degradation
   is in the safe direction by construction — on a slow P2P read `run_cmd` times out,
   `duration` is 0, and the file plays.
5. **Every path, especially the one that failed.** The gate runs in `prepare_stream` (after
   `_ensure_playable`, before the audio guard — so the language reselect starts from a vetted
   pick and reuses the probe it was about to pay for), inside `pick_audio_stream_verified` (the
   `--audio-lang` branch, where a placeholder's lone `und` track would otherwise be accepted on
   benefit of the doubt), and in the three cast reselects (`vet_cast_video`,
   `vet_cast_container`, `_reselect_cast_for_lang`). A proven-short candidate is dropped from
   `results` **in place**, so no later reselect can land back on it. Auto-pick only: a manual
   pick stays the user's explicit choice, like every other guard.
6. **No denylist. Verdict for the run only.** There is a seductive argument for persistence —
   a torrent's content is immutable, the infoHash pins it, and unlike the size shortfall there
   is no "it grows and becomes good" case. It does not hold. The proof here is a **ratio
   between two uncertain quantities**: the numerator can be ffmpeg's _bitrate estimate_ when an
   MP4's `moov` sits at the end of a barely-buffered P2P stream; the denominator is
   crowdsourced and, for a series, an average episode length. Banning a healthy torrent for 30
   days — perhaps a title's only dub — on that basis is precisely the mistake ADR 0025's
   post-scriptum already corrected once. **When the proof cannot bear the consequence, lower
   the consequence.**
   A future persistence would be legitimate only under the conjunction: the measurement taken
   on an HTTP debrid url with a complete `Content-Length`, **and** a Matroska container (header
   duration, not estimated), **and** a ratio below 0.05, **and** an expected runtime from a
   _movie_ meta rather than a series. Outside that, no.
7. **`sources_truncated`, not `no_playable_stream`.** A new error code is justified when the
   caller's reaction changes, and it does: `no_playable_stream` is documented as worth retrying
   later, while a truncated file is what the source _contains_ — the same command fails
   identically. Both measures (`duration_s`, `expected_runtime_s`) go out in clear so an
   implausible expected runtime is visible at a glance. On success the JSON carries
   `duration_verified` (`true` = measured and compatible; `null` = not checkable), the honest
   twin of `audio_verified`, read cache-only (`tracks.cached_duration`) so reporting never pays
   a probe of its own.

**Project rule this makes explicit:** when a decision needs a fact about the file, read it from
the probe you are already paying for. Three ADRs now share one ffprobe per stream per process.

## Consequences

- The incident's exact command now either reselects a plausible source or fails with
  `sources_truncated` — it can no longer report success over a 30-second clip.
- **Zero extra ffprobe on the happy path.** Every call site reads a `Tracks` it already
  produces. With an unknown runtime the guard does not even resolve a url. Do not add a shorter
  ffprobe timeout for the duration check alone: `timeout` is not part of `tracks._cache`'s key,
  so a second timeout would break memoization and double the probes.
- The reselect is entered only behind a _measured_ shortfall, and is capped at 2 alternatives
  (against the cast vets' 4): on P2P each extra candidate is another `engine.resolve` buffering
  wait for a title already proven fake.
- **Known worst case — a wrong Cinemeta runtime.** Anthology series and miniseries with
  variable episode lengths can advertise, say, 90 min against a legitimate 28-minute episode
  (ratio 0.31 → false positive). Mitigation is honesty rather than cleverness: both numbers are
  in the JSON, so the implausible expectation is immediately visible. If this shows up in the
  field, the lever is lowering the ratio to 0.25 — **not** adding a second weak signal.
- Unit tests must not hit the network for the runtime: a conftest fixture defaults
  `api.expected_runtime_s` to "unknown" (guard off), and tests that exercise the guard set a
  value themselves.
