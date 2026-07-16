# 0018. Subtitle sync: exact-file hash match + manual retime

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** project maintainer

## Context

Subtitle selection was a language lottery: `subs.pick_subtitles` sorted the addon's
response by preferred language and took the first track. The OpenSubtitles v3 addon's
plain response carries **no release metadata** (id/url/lang only — verified live on
Coherence: 6 interchangeable `ita` tracks), so nothing downstream can tell a track timed
for this release from one timed for a different cut or framerate. A wrong guess is off by
seconds (different source) or drifts progressively (23.976↔25 fps ≈ 4.3%).

State of the art (researched 2026-07-16): the identity-first fix is the **OpenSubtitles
moviehash** (`size + 64-bit LE checksum of first/last 64 KB`) — the REST API explicitly
recommends sending it and flags `moviehash_match` results first; the Stremio addon
protocol carries the same `videoHash`/`videoSize`/`filename` extras. Verified on
opensubtitles-v3: a hash query returns ONLY the exact-file matches. Where no hash match
exists, Bazarr-style weighted metadata scoring needs response metadata this addon does
not expose. Desync classes: constant offset, linear fps drift, piecewise cuts — a single
delay knob covers only the first. Audio-anchored aligners (alass, ffsubsync) are the
modern local fallback but add an external tool and runtime cost.

Delivery constraint: mpv can retime at runtime (`z`/`x`); the Chromecast caption track is
fixed at LOAD. Any correction must therefore land in the FILE, upstream of both paths
(policy parity — same lesson as ADR 0017).

## Decision

1. **Hash-first matching** (`oshash.py`): compute the stream's moviehash with two ranged
   HTTP reads (~128 KB) on the resolved url; query each subtitles addon ALSO with the
   `videoHash`/`videoSize`/`filename` extras (`filename` from Torrentio's
   `behaviorHints`); tag those results `hash_match`. Ranking: language remains the
   primary key (a synced track in the wrong language helps nobody), hash-match wins
   within a language. Everything is best-effort: no Range support / tiny file / network
   error → today's behaviour, silently.
2. **Manual retime** (`--sub-offset SEC`, `--sub-fps SRC:DST`): `t' = t·scale + offset`
   applied in place to the downloaded SRT before any delivery — mpv and the cast's
   WebVTT see identical corrected timings. Two parameters cover the constant-offset and
   fps-drift classes; piecewise cuts stay out of scope.
3. **Honest reporting**: the headless JSON gains `subtitles_match` — `"hash"` (sync
   verified by construction) vs `"lang"` (best guess) — mirroring `audio_verified`.

## Consequences

- A hash-matched subtitle is synchronized by construction; the happy path costs two
  64 KB reads and one extra parallel addon query.
- No hash match → same guess as before, but now _labelled_ as a guess, and correctable
  with the retime flags without leaving nstream.
- Audio-anchored auto-sync (alass on the Tier-2 path, where the full file is already on
  disk) is deliberately deferred to its own ADR if field use shows hash misses are
  common — measure before optimizing.
- Changing the retime on a running cast requires a re-cast (caption track is fixed at
  LOAD); resume makes that cheap.
