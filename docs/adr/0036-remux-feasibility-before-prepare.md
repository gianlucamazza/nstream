# 0036. Remux feasibility is decided before the prepare; never a mute cast

- **Status:** Accepted
- **Date:** 2026-10-01
- **Deciders:** maintainer

## Context

Field incident, 2026-10-01: a headless cast of a 66.6 GB 2160p `.mkv` whose audio was
Dolby/DTS printed "preparo il file intero", then `remux.remux_to_file` found 52 GB free and
refused. `cast_flow.run_cast` degraded to a direct cast of the undecodable file. The TV played
with no audio track (`active_tracks: []`), and the JSON still said `ok: true`,
`audio_lang: "ita"`, `audio_verified: true`.

Several things went wrong at once:

- **The disk check came too late.** It ran inside `remux_to_file`, after the decision.
- **The ranking ignored free disk.** `quality.score_components` used only the configured
  `cast_remux_max_size_gb`, and `cast_vet._reselect_cast_for_lang` took the first remux in
  rank order whatever its size.
- **The mirror fallbacks were off without a word.** ADR 0015/0022 rely on the mirror, and
  `mirror.available` requires `hyprctl`, which is absent on a non-Hyprland desktop.
- **The outcome reported a dropped plan.** `CastOutcome` copied the abandoned remux plan's
  language as if it were playing.
- **A refused media load fell back to catt.** `cast_delivery.drive_bridge` fell back to catt
  even on a pre-start `receiver_error`, so catt's exit code could turn a refused load into a
  start.

## Decision

nstream decides whether a Tier-2 remux can run (`remux.refusal`) before committing to it:

- **Disk:** enough free space for the release size plus 10%, measured after stale remuxes
  are reaped.
- **Size cap:** a release over `cast_remux_max_size_gb` is refused only when nobody can
  confirm it.
- **ffmpeg and config:** ffmpeg is present and `cast_remux` is on.

When the remux would be refused, `run_cast` tries these in order:

1. the mirror, unless `--no-mirror`;
2. a verified direct release in the preferred languages (`cast_vet.find_instant_direct`);
3. otherwise it raises `CastRemuxInfeasible`. Headless reports it as `remux_infeasible`.

It never casts the undecodable file as-is.

If a remux that passed the check still fails mid-way, the direct-cast fallback remains, but the
outcome reports `audio_lang: null` and `audio_verified: false` (`CastOutcome.audio_degraded`).

The ranking applies the same budget (`quality.remux_size_budget`). For cast it also puts the
preferred language and `direct_cast` above resolution.

A pre-start `receiver_error` is a not-started outcome. Only transport failures fall back to catt.

## Rationale

| Option                                                            | Verdict                                                                      |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Keep the late guard and the direct-cast fallback                  | Rejected: produces a mute TV reported as success, the ADR 0031 failure class |
| Always fail when a remux is refused                               | Rejected: a mirror or a verified direct release is a real alternative        |
| Decide feasibility up front, with stand-ins and an honest failure | **Chosen**                                                                   |

Ranking against the disk prevents most refusals from ever arising. The refusal check remains
the guarantee.

## Consequences

- New headless error code `remux_infeasible`, carrying a `reason` field. The TUI backs out to
  the list with the same reason.
- `mirror.unavailable_reason` explains a disabled mirror, and `--doctor` lists it.
- Ranking now depends on free disk space. Tests stub `util.free_gib`.
- The `remux_to_file` guards stay as defense in depth. The size-cap prompt still applies
  interactively.

## References

ADR 0005, 0015, 0022, 0031, 0035. Symbols: `remux.refusal`, `cast_flow.run_cast`,
`cast_flow.CastRemuxInfeasible`, `quality.remux_size_budget`, `quality.score_components`,
`cast_vet._reselect_cast_for_lang`, `cast_delivery.drive_bridge`, `bridge.cast_load`.
