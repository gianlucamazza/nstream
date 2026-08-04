# 0029. One continuation policy, one advance decision

- **Status:** Accepted
- **Date:** 2026-08-04
- **Deciders:** project maintainer

## Context

"What plays next?" was answered in three places that disagreed, and "did this episode finish?"
in four.

**Three continuation paths.**

1. `series.binge` advanced by index arithmetic over the episode list, gated by `next_label`.
2. `headless._run_auto_resume` merged two lists (`state.recent` + `state.watched_series`) by
   hand and ranked them differently per branch: **with** a search term the in-progress list won
   unconditionally, so a half-watched S01E02 from weeks ago beat a binge finished minutes
   earlier; **without** one, the two were compared by `ts`.
3. `cli.run_continue` did not advance at all. It read `state.recent`, which hides watched
   entries, so a finished episode vanished from the TUI's continue-watching list and the series
   had to be found again through search. Headless advanced past the credits; the TUI replayed
   them or lost the series.

Also: an entry with no usable season/episode (legacy, or a fire-and-return `note_started`)
compared as `(0, 0)`, so the "next" episode came out as S01E01 — silently restarting a series
the user was in the middle of.

**Four delivery backends, one of which was right.** `advance` was computed inside each backend:
the direct cast derived it from the position (≥ `CAST_DONE`), while the Tier-2 remux
(`remux.cast_file`) and the mirror (`mirror.cast_via_mirror`) returned `False` **hardcoded** —
`next_label` never even reached them. The remux carried a comment justifying it ("a remux is a
single movie"), which stopped being true the day the binge learned to cast. Field consequence,
found 2026-08-04 while casting The Punisher S01E01: any release whose audio needs a remux
(Dolby/DTS — the common case) or that falls back to the mirror **ends the binge after one
episode**, silently.

**Two thresholds that never met.** `history.WATCHED_THRESHOLD = 0.9` (plus a 60 s tail) and
`cast_delivery.CAST_DONE = 0.97` both answer a question about "finished", in modules that do
not know about each other. The mechanism only works because 0.97 ≥ 0.9 — by luck, not by
contract.

**And an honest hole.** A fire-and-return cast (the headless default) returns as soon as the TV
has the media. Nothing observes where playback got to, so history keeps `duration = 0`, which
`is_watched` can never call finished — `-c` proposes the same episode forever.

## Decision

Three claims, three homes. **No new module**: `headless` already imports `series`, the edge is
one-directional and legal, and `series` is the series domain by ADR 0009. A `continuation.py`
would add a graph node to host ~25 lines that belong there.

1. **The next video is decided by `series.next_up`** (with `series.next_video` as its pure
   ordering half), consumed identically by `headless._run_auto_resume`, `cli.run_continue` and
   `series.resume`. The order of its cases _is_ the policy: a film or an unfinished episode
   resumes; an entry that does not know where it sits in the series resumes (it cannot
   advance — this is the S01E01 restart bug, closed); an unreadable episode list resumes (a
   catalogue hiccup must not block playback, the ADR 0028 §4 stance); a finished episode
   advances, or reports the series completed. Season boundaries are not a special case: the
   successor is the first `(season, episode)` tuple strictly greater, so S01E13 → S02E01 falls
   out of the same comparison. `series.binge` derives its next episode from the same function
   instead of `idx + 1`.

2. **`state.resumable` is the only "what can I continue?" query** — `recent()` plus finished
   _series_ episodes (a finished film is done), one ordering by `ts`. The branch bug is closed
   by construction: with a single ordered list there are no two lists to reconcile by hand.
   `state.is_watched` becomes public so the policy can answer for one entry instead of
   inferring it from which list the entry arrived in.

3. **`advance` is computed once, in `cast_flow`**, from `cast_delivery.is_finished(pos, dur)` —
   the single definition in the repo. `advance = bool(next_label) and is_finished(pos, dur)`.
   The backends no longer produce it, and **no longer receive `next_label`**: their tuples lost
   the field entirely rather than keeping a dead one, because a field nobody computes is
   exactly the vector that produced the hardcoded `False`. _Corollary, in the ADR 0021 mold: a
   new delivery backend does not receive `next_label`; if it does, that is a review defect._
   A backend reports what it observed; only this layer knows whether an episode follows.

4. **The two thresholds coexist; the invariant is written down.** They answer different
   questions — "is there a resume point left?" (tolerant: mpv parked at EOF, padded durations)
   versus "was that `ended` a natural end or a manual stop?" (severe: advancing on a deliberate
   stop is worse than not advancing) — so they are not collapsed into one number. What is
   pinned by a test is the implication: **`CAST_DONE ≥ WATCHED_THRESHOLD`, i.e. advance implies
   watched.** Inverted, a cast that counted as finished would stay out of `watched_series` and
   `-c` would replay the episode forever.

5. **Fire-and-return is reconciled from measurement, never from an estimate.** Before deciding
   what to continue, `-c` asks the receiver of a live cast session once
   (`state.cast_session_device` → `caster.status` → `state.update_from_receiver`) — the same
   merge `--stop` and `--status` already perform, at the one call site where the answer changes
   a decision. No discovery: without a session it costs nothing. Writing the expected runtime
   into `duration` was considered and rejected — a measured field everywhere else would become
   an estimate in the one place that governs `is_watched` for the whole history, which is
   exactly what ADR 0028 §6 refuses.

## Consequences

- A binge on the TV survives a Dolby/DTS remux and the mirror. This is the user-visible fix.
- `-c` answers the same way in the TUI and headless: a finished episode continues with the next
  one, marked "→ prossimo episodio" in the list rather than disappearing.
- The parity is now **structural**, not disciplinary: remux and mirror cannot forget the advance
  because they do not compute it. The regression test that would have caught the original bug
  exists (`tests/test_cast_flow.py::test_advance_parity_across_backends`, verified to fail
  against a re-introduced hardcoded `False`).
- Honest limits, unchanged by this ADR and deliberately not papered over:
  - A TV switched off before any `--stop`/`--status`/`-c` leaves no final position; `-c` will
    propose the same episode. The alternative is inventing a duration.
  - On the mirror `dur` comes from mpv, and on the remux's catt branch from a periodic poll: the
    last observed position can sit under the threshold if the poll missed the final interval.
    That is a false **negative** (a binge that stops), never a false positive.
  - `api.episodes` still drops `season == 0`, so specials are invisible to the continuation.
    Including them would change the `(season, episode)` ordering the whole advance rests on
    (S00 sorts before S01, so a binge would start from the specials). A known gap, not fixed in
    passing.
  - `series.binge` still stops on an episode with no sources rather than skipping it. A binge
    that silently jumps over an episode is worse than one that stops and says why; changing it
    must be a declared decision, not a side effect.

## References

- ADR 0009 — the series flow and its injected player (why the policy lives in `series`).
- ADR 0011 — the structural precedent: shared policy extracted, mechanics left to the backends.
- ADR 0021 — the same shape: an invariant that must hold on _every_ path, with the corollary
  spelled out so a new path cannot quietly opt out.
- ADR 0028 — the honesty rule this one inherits: never synthesize a measured value.
