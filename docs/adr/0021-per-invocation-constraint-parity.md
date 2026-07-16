# 0021. Per-invocation constraints hold across every selection path

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** project maintainer
- **Closes:** the "quality parity" known gap recorded in ADR 0017

## Context

The per-session quality choice (`--quality` → `FilterSpec.exact_resolution`) was honored
only by the PRIMARY pick (`prepare_stream`). Every cast reselect path — video-codec
vetting (0017), the audio-language reselect, the in-cast dub switch — re-ranked through
`_cast_playable`, which hardcoded `exact_resolution=0`. The same parity defect bit three
times in a single day: the ITA-dub reselect returned a DivX rip (fixed for VIDEO by
0017), then a 4K release that flipped the cast to the ADR-0015 auto-mirror despite
`--quality 1080`, then the forced `--audio-lang` pick (which itself honors quality) was
re-vetted through the unfiltered path and flipped to mirror again. The field workaround
— temporarily editing the user's config to disable the auto-mirror — is exactly the
class of fix this ADR eliminates.

## Decision

1. **Invariant**: every stream (re)selection receives the RESOLVED per-invocation
   constraints. A new selection path MUST take `exact_resolution` explicitly — an
   unfiltered `_cast_playable` call is a review defect, and `_cast_playable`'s docstring
   says so.
2. **Carrier**: at the two post-`prepare_stream` boundaries (`cli._play_video`,
   `headless._auto_play`) the callers `replace(opts, quality=vetted.quality)` — the
   series-binge precedent. Downstream of `prepare_stream`, `PlayOpts.quality` is always
   a resolved int (0 = Auto, N = exact); `None` exists only before resolution
   (documented on the dataclass). `run_cast` derives `exact` once and threads it to
   `vet_cast_video`, `vet_cast_audio`→`_reselect_cast_for_lang`, `cast_languages`,
   `cast_resolver`.
3. **Empty filtered pool** → today's honest fallback (absent dub → safety subtitles; no
   video alternative → mirror/error). An exact quality choice is a hard user constraint:
   no silent relaxation.
4. **Per-invocation mirror override**: `PlayOpts.mirror` becomes tri-state — `None`
   (config decides; the ADR-0015 auto-switch may apply), `True` (`--mirror`), `False`
   (new `--no-mirror`: suppresses the auto-switch for this invocation). With
   `--no-mirror` and video the DMR can't render, the cast fails explicitly
   (`CastVideoUnsupported`): manual intent always wins.
5. Informational listings (`available_resolutions`, `available_audio`) stay unfiltered
   by design — they enumerate the CHOICES, not the selection.

## Consequences

- `--quality` now means what it says on every path; the tonight-class mirror flips
  cannot happen from a reselect.
- No config editing as an operational lever: the auto-mirror is per-invocation
  suppressible.
- Test-enforced: parity pins on each path, a threading spy on `run_cast`, boundary pins
  on both callers, and `--no-mirror` scenario pins.
