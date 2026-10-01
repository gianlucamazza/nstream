# 0041. With the live tier, a soft language preference no longer yields to another dub

- **Status:** Accepted
- **Date:** 2026-10-01
- **Deciders:** maintainer
- **Amends:** 0035 (when the instant direct cast in another language is used)

## Context

ADR 0035 starts a verified direct cast in a later `audio_langs` language when the preferred
dub needs a whole-file remux. The wait it avoided was real: on 2026-10-01 the Italian AC-3
track of "Eternal Sunshine" needed 3.3 GB at ~2.4 MB/s, about 15 minutes. The trade-off
was also visible: the user got English instead of Italian, and the instant pick was an
untagged MP4 with burned-in Chinese subtitles (issue #6).

ADR 0039 (accepted the same day) removes the wait: the conversion streams as live HLS-TS
and the TV starts in seconds, in the preferred dub.

## Decision

`cast_flow._cast_would_wait` is False when `remux.live_feasible` holds: the preferred dub
starts live and `_defer_to_instant` (ADR 0035) does not trigger. The ADR 0035 fallback stays
for when the live tier cannot run (`cast_live` off, ffmpeg or castbridge missing, the live
window does not fit on disk). Its notice now names the way to wait for the preferred dub:
`--audio-lang <lang> per attendere`.

## Rationale

| Option | Preferred dub | Start | Risk |
| --- | --- | --- | --- |
| ADR 0035 as is | after ~15 min, or never (another dub) | instant | a worse release is picked to start fast |
| **Live first (this ADR)** | yes | seconds | stereo instead of 5.1 on the live path |

## Consequences

- Soft preferences behave like `--audio-lang` whenever the live tier can run.
- The live start may still fail (receiver, producer). It then falls back to the complete
  file, not to another dub. The language choice was already made at that point.

## References

ADR 0035, ADR 0039. Symbols: `cast_flow._cast_would_wait`, `cast_flow._defer_to_instant`,
`remux.live_feasible`, `cast_vet.instant_defer_notice`.
