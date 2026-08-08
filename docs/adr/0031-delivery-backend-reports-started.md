# 0031. A delivery backend reports whether the cast started; `ok: true` requires it

- **Status:** Accepted
- **Date:** 2026-08-08
- **Deciders:** project maintainer

## Context

Field evidence, 2026-08-08: a headless cast printed `nstream: cast non riuscito` on stderr and
emitted, on stdout, with exit code 0:

```json
{"ok": true, "action": "cast", "stream": {"resolution": 0, "codec": "", "size_gb": 0.0, ...}}
```

The `--json` contract is the interface every agent-driven caller reads. A failed cast that
declares success is worse than a crash: nothing downstream can detect it.

**The fact is destroyed at the failure site.** `caster._cast_via_catt`
(`src/nstream/caster.py:476-499`) logs the failure, prints to stderr, and returns `(0.0, 0.0)` —
the _same_ value a legitimate fire-and-return returns by design (`caster.py:496-499`), because a
fire-and-return has no observed position. The two are then indistinguishable to every caller.
`caster.cast` (`:323-335`) merely widens the tuple.

**Nothing above can reconstruct it.** `CastOutcome` (`src/nstream/cast_flow.py:85-105`, built at
`:338-345`) has no success/failure field at all; `run_cast`'s only failure channel is raising
`CastVideoUnsupported` or letting `ContentTooShort` propagate. `headless_play.auto_play` catches
exactly those two (`src/nstream/headless_play.py:297-311`), unpacks the outcome with no check
(`:312-321`), and hardcodes `ok: true` at `:368-371` with `return 0` at `:409`. There is no
branch between the `run_cast` call and that emit that can set `ok: false`.

**The one existing signal is `--follow`-only.** `on_cast_event`
(`src/nstream/headless_play.py:271-281`) does emit `{"ok": false, ...}` on a `failed` event — but
it is wired only when `args.follow` (`:295`), it is an _extra_ JSONL line, and the final summary
still says `ok: true`. In fire-and-return (the headless default, and the reported case) the event
is dropped entirely.

**It is not one backend.** `mirror.cast_via_mirror` (`src/nstream/mirror.py:305-345`) has five
early returns of `(0.0, 0.0)` — sender unavailable, no headless output, mpv missing, window never
appeared, teardown — and `remux.cast_file` (`src/nstream/remux.py:447-467`) has the `catt non
trovato` and `_await_start` failures. Three backends, one contract, the same hole in each.

Side effect of the silence: `state.remember_cast` (`src/nstream/headless_play.py:336`) records a
cast session for a cast that never started, and `describe_stream` reports the all-zero `stream`
block observed in the field.

This is the mirror image of ADR 0029. There, `advance` was computed _inside_ backends that got it
wrong; the fix moved the decision up and left the backends reporting only what they observed.
Here the backends observe something decisive — "the receiver never took the media" — and throw it
away. Same tier, same principle, opposite direction.

## Decision

**A delivery backend reports whether the cast started. The layer above decides what that means.
`ok: true` is emitted only for a cast that started.**

1. **`CastResult` NamedTuple in `cast_delivery`**, which already owns `BridgeOutcome`
   (`src/nstream/cast_delivery.py:19`) and is the shared delivery tier by ADR 0011:

   ```python
   class CastResult(NamedTuple):
       pos: float
       dur: float
       subs_delivered: bool = False
       started: bool = False  # the receiver accepted the handoff / was observed playing
       error: str | None = None  # "catt_missing" | "cast_timeout" | "cast_failed" | …
   ```

   **Consumers read it by attribute, never by unpacking.** A NamedTuple's defaults govern
   _construction_ arity, not _unpacking_ arity — `len(CastResult(...))` is always 5, so
   `pos, dur = CastResult(1.0, 2.0)` raises `ValueError`. Field order buys nothing, and every
   current call site unpacks positionally (`cast_flow.py:244, :271, :284, :314`; `cli.py:311`).
   They all migrate to `r.pos` / `r.started`, which is the pattern `BridgeOutcome`
   (`src/nstream/cast_delivery.py:43`) already established in this repo — no consumer of it
   unpacks positionally — and the only form immune to a future field.

   **`caster.py:335` stops splatting.** `return (*catt_result, bool(sub_paths))` is the single
   `*result` splat in the repo: against a 5-field `CastResult` it yields a flat 6-tuple, losing
   the NamedTuple _and_ the arity **with no exception at the return site**, surfacing far away at
   `cast_flow.py:314`. It becomes explicit field construction.

2. **All three backends adopt it** — `caster`, `mirror`, `remux`. The parity is part of the
   decision, not an implementation detail: fixing only `caster` leaves the defect alive on the
   two paths a Dolby/DTS title actually takes.

3. **`started=True` on fire-and-return means the handoff was accepted** (catt returned rc 0),
   not that playback was observed. That is the strongest evidence available without a poll loop.
   Tightening it would make every fire-and-return cast a false negative — which is the failure
   mode this ADR exists to avoid, inverted.

4. **`CastOutcome` gains `started` and `cast_error`**, threaded from whichever backend ran.
   `run_cast` does **not** raise: the interactive path wants the outcome and the backends have
   already printed to stderr.

5. **`headless_play.auto_play` emits `{"ok": false, "error": "cast_failed", ...}` and returns 1
   when `not outcome.started`** — placed _before_ `state.note_started` / `state.remember_cast`
   (`src/nstream/headless_play.py:328-336`), which closes the phantom-session bug in the same
   move. `cast_failed` is a new code in the headless taxonomy; nothing existing means "the pick
   was fine, the delivery never started". The `--follow` inconsistency disappears with it: the
   JSONL `failed` line and the final summary now agree.

6. **`cli._play_on_cast` must not save a resume entry for a cast that never started**
   (`src/nstream/cli.py:180`).

## Rationale

The alternative was an upstream check in `cast_flow` or `headless_play`, leaving the backend
contract alone. Rejected: the fact "catt exited rc≠0 / the receiver never started / mpv's window
never appeared" exists exactly once, at the failure site, and is **unreconstructible upstream**.
Any upstream check is either a heuristic — `pos == 0` is also true for every legitimate
fire-and-return — or a race, re-polling `catt info` after the fact.

The cheaper variant — always wire a `cast_flow`-owned `on_event` wrapper so the `failed` event
arrives without `--follow` — was also rejected: `mirror.cast_via_mirror` emits no events at all
and `remux.cast_file`'s never-started branch emits none either. It would fix one path of three and
leave the parity unmet.

**A trap this ADR names explicitly.** The four tests at `tests/test_caster.py:353-382`
(`test_cast_launch_failure_returns_zero`, `test_cast_launch_timeout_degrades`,
`test_cast_gives_up_if_never_starts`, `test_cast_poll_timeout_counts_as_unreachable`) currently
pin `(0.0, 0.0)`-on-failure _as intended behaviour_. They pin a **sentinel value** — an
implementation accident — not a guarantee any caller depends on, so they are rewritten rather
than honoured. And because `CastResult` is a NamedTuple, `== (0.0, 0.0, False)` still passes:
left untouched those tests would stay green while asserting nothing about the new field. Each
keeps its real guarantee (no exception raised, bounded `_CAST_GIVEUP` polls, exactly one `failed`
event) and gains an explicit `assert result.started is False and result.error == "…"`.

## Consequences

- A failed cast is reported as a failure. This is the user-visible fix and the reason for the ADR.
- Callers of the `--json` contract must handle a new error code, `cast_failed`, carrying
  `cast_error` and `device`. `skills/nstream/SKILL.md:181-215` must list it.
- No more phantom cast sessions: `-c` stops proposing a resume for a cast that never played.
- Obligation on future code, in the ADR 0021 / 0029 mold: **a new delivery backend returns a
  `CastResult` and sets `started`. A backend that returns a bare tuple, or hardcodes
  `started=True`, is a review defect.** The parity is structural — the layer above reads one
  field and cannot be taught to ignore it.
- Accepted cost: `started=True` on handoff means a receiver that accepts the media and then fails
  silently still reports success. Detecting that requires a poll loop the fire-and-return mode
  deliberately does not have. A false **negative** on delivery is not introduced; the remaining
  gap is a false positive at the receiver, unchanged by this ADR.
- The tests that pin the old contract are rewritten, not deleted. Anyone bisecting through this
  change sees the guarantee move, not vanish.
- **Positional consumption of a delivery result is now a review defect.** Tuple unpacking and
  `*result` splatting both break silently or at a distance when a field is added; attribute
  access does not. `caster.py:335` was the one splat in the repo and it is removed here — a new
  one would re-open exactly this class of bug.
- A second trap, distinct from the `test_caster.py` one: the `cast_flow` tests stub the backends
  with lambdas returning **plain tuples** (`tests/test_cast_flow.py:828, :833`). Left alone they
  keep satisfying 3-tuple unpacking and stay green while production breaks. The stubs migrate to
  `CastResult` in the same change.

## References

- ADR 0029 — the direct companion: a backend reports what it observed, the layer above decides.
  Same tier, same reasoning, opposite direction (there a decision was wrongly _in_ the backend;
  here an observation was wrongly _discarded_ by it).
- ADR 0011 — why `CastResult` belongs in `cast_delivery`: shared delivery policy extracted,
  mechanics left to the backends.
- ADR 0021 — the corollary form used in Consequences, so a new path cannot quietly opt out.
- `src/nstream/caster.py:323-335`, `:476-499`, `:528-530`, `:549-551`;
  `src/nstream/mirror.py:305-345`; `src/nstream/remux.py:447-467`;
  `src/nstream/cast_flow.py:85-105`, `:338-345`;
  `src/nstream/headless_play.py:271-281`, `:295`, `:297-321`, `:328-336`, `:368-371`, `:409`;
  `src/nstream/cli.py:180`; `tests/test_caster.py:353-382`.
- Field evidence: session 2026-08-08, `--cast "Le vite degli altri"` → stderr "cast non riuscito"
  with `{"ok": true}` on stdout, exit 0.

## Appendix — an invariant restored, not decided (no separate ADR)

The same session produced `KeyError: 'url'` at `src/nstream/cast_flow.py:257`, surfaced as
`{"ok": false, "error": "internal"}` via `src/nstream/cli.py:932-947`. It is recorded here rather
than in its own ADR because the fix **restores** a stance ADRs 0017 / 0022 / 0028 already
declared, instead of deciding anything new.

`stream_select._playable_url` (`src/nstream/stream_select.py:340-355`) catches
`engine.EngineUnavailable` and returns `None` **without writing a `url` key**. Every `cast_vet`
gate then reads "no url" as "unprobeable → benefit of the doubt": `_cast_video_codec`
(`src/nstream/cast_vet.py:73-77`) → `""` → castable; `_duration_castable` (`:87-94`) → `True`
(via `availability.vet_duration`, which short-circuits on an empty url);
`_cast_audio_tracks` (`:67-70`) → `[]` → `_cast_plan_for` (`:214-215`) → an unverified `direct`
plan. The candidate survives as the `tagged_guess` last-resort tier (assigned `:306`, returned
`:307`), wins, and `run_cast` dereferences `chosen["url"]` unguarded at
`cast_flow.py:245, 257, 285, 315` — while `:198` already reads it defensively.

There are three candidate loops, not one: `vet_cast_video` (`:118-131`), `vet_cast_container`
(`:178-205`), `_reselect_cast_for_lang` (`:268-306`). Two further sites share the flaw and are in
scope: `vet_cast_container:197`, whose `if not target_lang or (…)` short-circuit lets `:204`
return an unresolved stream when no target language is set (with one set, `plan.verified` already
protects it); and `cast_resolver.resolve` (`:33-37`), which hands back `_playable_url`'s `None`
instead of walking to the next candidate. The two entry probes on `chosen` itself
(`vet_cast_video:115`, `vet_cast_audio:323`) sit outside every loop and need the same gate, or
the invariant holds only for reselected candidates.

The stance those ADRs took is that the benefit of the doubt is owed to a stream that is
**resolved but unprobeable** (no ffprobe, probe failed) — never to one that could not be resolved
at all. `engine._wait_buffer` (`src/nstream/engine.py:338-377`, `_BUFFER_TIMEOUT = 120`) raises
`EngineUnavailable` correctly on a dead swarm; the abort mechanism exists and the consumer
misreads it.

The invariant, to be enforced where it lives and commented there rather than restated as policy:

- `cast_vet` resolves each candidate's url **after the probe-cap break and after `probed += 1`**,
  and `continue`s on `None` before any castability gate runs. After that, `""` and `[]` can only
  mean resolved-but-unprobeable — the distinction holds by construction, with no tri-state to
  keep in sync.

  Not "at the top of the loop": `stream_select.py:294-298` deliberately checks the cap _before_
  resolving ("resolving an over-cap candidate can cost a P2P buffering wait … for a stream we'd
  discard anyway"), pinned by
  `tests/test_stream_select.py::test_pick_audio_verified_cap_checked_before_resolving`. Hoisting
  the resolve would violate that doctrine. Placing it after `probed += 1` also settles the budget
  question explicitly: **an unresolvable candidate consumes probe budget.** The loop then walks
  exactly as far as it does today, and a dead swarm stops early instead of scanning the whole
  ranking.

- `cast_flow` asserts the invariant at the single point where the stream settles (immediately
  after `chosen = plan.stream`, above the ADR 0022 settled-container check) and raises
  `CastStreamUnresolved`, which `headless_play` reports as `no_playable_stream` — already
  documented as the retry-worthy code, the right semantics for a dead swarm. One honest failure
  replaces four crash sites.
- A negative resolve is memoised on the `Stream` dict for the run, so a dead swarm costs one
  120 s wait per distinct candidate instead of one per gate. This is the symmetric half of what
  `_playable_url` already does on success (`stream_select.py:347, :351` mutate the dict), so the
  saving lands exactly on the failure path, which is the only one paying repeatedly today: 2
  resolves per candidate in `vet_cast_video`, up to 4 in `vet_cast_container`, 3-4 in
  `_reselect_cast_for_lang`.
- No existing test constructs a url-less `Stream` — in `test_cast_vet.py`, `test_cast_flow.py` or
  `test_headless.py`. That absence is why the crash shipped, and it means the gate is a no-op for
  the suite as it stands: it proves nothing until the missing fixture is added.

Known residual, recorded and **not** fixed here: even memoised, `probe_cap` 6 + 4 + 4 against a
fully dead swarm still costs roughly fourteen 120 s waits. The real answer is a run-scoped resolve
deadline — a separate change with its own risk profile, and its own decision.
