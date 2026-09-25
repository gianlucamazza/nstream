# 0035. A soft language preference yields to a direct cast

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** project maintainer

## Context

ADR 0005 remuxes Dolby/DTS audio into a complete MP4 before the Default Media Receiver
can start, and ADR 0022 does the same for a Matroska container. Both waits are real:
Google's [supported media](https://developers.google.com/cast/docs/media) still lists
MP4 and WebM, and AC-3 passthrough only through the Web Receiver SDK, not the bare
Default Media Receiver. Streaming the remux was tried and stays black.

`vet_cast_audio` then treats "the primary language exists somewhere in this release" as
a finished remux plan. A verified direct cast in a later `audio_langs` entry — MP4 or
WebM, first track already decodable — never gets compared, so an 11-minute fetch wins
over a cast the TV can read from the source immediately.

ADR 0013 would let a registered receiver pass AC-3 through and pick a track. It does
not make Matroska loadable, and it is still blocked on CAF registration.

## Decision

When audio was not forced with `--audio-lang`, `cast_flow.run_cast` keeps a full-file
prepare only if `cast_vet.find_instant_direct` finds nothing at the requested quality.
The search prefers the earlier `audio_langs` entry. A direct hit in the primary
language replaces the remux with no question. A direct hit in a later language does
too on a headless run, with safety subtitles in the primary language and a `notice`
that names the container, the codec and the language that actually starts. On a TUI
the later-language shortcut asks once; declining keeps the remux.

`--audio-lang` does not consult `find_instant_direct`.

## Rationale

| Option | Verdict |
| --- | --- |
| Keep remux whenever the primary dub exists | Faithful dub, multi-minute prepare even when an MP4 is sitting in the list. This is the bug. |
| Direct cast first, soft language second (chosen) | The TV starts at once when a loadable file exists. The preferred dub still wins when it is itself direct, and when the user named `--audio-lang`. |
| Stream the remux | Already rejected (ADR 0005). Current Cast docs do not reopen it for the Default Media Receiver. |
| Finish ADR 0013 instead | Right for AC-3 passthrough. It does not remove the Matroska rewrap, and it is blocked. |

Headless does not ask. A fire-and-return caller cannot sit on a prompt; the `notice`
is the record. That matches `cast_mirror_over_remux_gb`, which already prefers starting
over a huge prepare, but only past 10 GB.

## Consequences

- A title whose only primary-language copy is MKV or AC-3 still downloads the whole
  file before playback. The shortcut exists only when a verified direct candidate does.
- `audio_lang` in the cast JSON is the dub that started, which may be a later
  `audio_langs` entry. `notice` explains the deferral. Agents must read both.
- The remux progress line is printed even when stderr is not a tty, at 10% steps.
- An unverified or `multi` release whose real first track is a different language
  cannot win the shortcut (the Independence Day regression stays closed).

## References

- `cast_vet.find_instant_direct`, `cast_vet.instant_defer_notice`, `cast_flow.run_cast`, `remux._run_ffmpeg`
- ADR 0005, ADR 0022, ADR 0013
- https://developers.google.com/cast/docs/media
