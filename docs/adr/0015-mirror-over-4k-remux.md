# 0015. Prefer the realtime mirror over a 4K remux for Dolby-only releases

- **Status:** Accepted
- **Date:** 2026-07-14
- **Deciders:** project maintainer

## Context

When a title's only playable release is **4K with Dolby/DTS audio and no AAC alternative**, the
Default Media Receiver can't decode the audio, so Tier-2 remux (ADR 0005) kicks in — and a 4K
remux means downloading the whole 30-60 GB file to transcode only the audio, a long prepare
wait. `quality.py` already demotes likely-remux 4K releases via `cast_remux_max_resolution`
(default 1080) and `remux_within_size`, but when the _only_ live option is a 4K Dolby file (see
ADR 0014), that guard has nothing better to fall to and the huge remux proceeds.

nstream already owns a realtime path that sidesteps this entirely: the **headless mirror**
(ADR 0006, `src/nstream/mirror.py`) — mpv decodes the stream locally (Dolby included) and the
openscreen Cast Streaming sender mirrors a 1080p H.264 window to the TV, **starting in ~seconds
with no download**. Today the mirror only engages on explicit `--mirror` for a remux-audio plan
(`cast_flow.run_cast`); it is never chosen automatically over a pathological remux.

## Decision

When the committed cast plan needs a remux **and** the release is above a resolution/size
threshold (a 4K Dolby-only pick), auto-prefer the mirror over the remux: instant start, no
multi-GB fetch, at the cost of 1080p SDR and ~120 ms latency. Below the threshold (a modest
1080p Dolby file) the remux still wins — it preserves native video and has no latency once
prepared.

## Rationale

| The one live option is… | Remux                                       | Mirror                               |
| ----------------------- | ------------------------------------------- | ------------------------------------ |
| 1080p Dolby, small      | native video, short prepare — **preferred** | 1080p SDR, latency — worse           |
| 4K Dolby, no AAC alt    | 30-60 GB fetch + wait — pathological        | instant, no download — **preferred** |

The mirror is the strictly better trade exactly when the remux is pathological (huge fetch),
and the strictly worse trade otherwise. A size/resolution threshold picks between them — no new
mechanism, just a routing rule over two paths that already exist.

**Relationship to ADR 0013 (custom receiver):** if 0013 ships and the display supports Dolby
passthrough, the DMR-can't-decode premise dissolves and this rule never fires for that device —
so 0015 is **not** technical debt but the correct fallback policy for **non-passthrough
devices** (a plain Chromecast dongle on a non-Dolby TV), where even a custom receiver can't
decode Dolby and the choice is genuinely remux-vs-mirror. The two ADRs compose: 0013 removes
the need where the hardware allows, 0015 makes the best of it where it doesn't.

## Consequences

- `cast_flow.run_cast` grows a routing branch: `needs_remux and resolution/size ≥ threshold and
mirror.available()` → mirror instead of remux. New config knob (e.g.
  `cast_mirror_over_remux_gb`, default ~10) so the threshold is tunable and the behaviour
  opt-outable.
- The mirror's costs (1080p SDR, latency, a headless mpv + null sink) apply to these casts —
  acceptable versus a 30-60 GB download, but surfaced to the user in a notice.
- No new external dependency: both paths are in-tree (`remux.py`, `mirror.py`).
- **Testing:** `cast_flow` unit tests asserting the threshold routing (below → remux, above →
  mirror), and that `--mirror`/`cast_mode` still override.

## As built (2026-07-14)

- New config knob `Config.cast_mirror_over_remux_gb` (default **10**, bounds `(0, 1000)`, `0` =
  never auto-switch). `cast_flow._remux_is_pathological(info, threshold_gb)` is the trigger:
  `True` when the parsed `size_gb ≥ threshold`, or (size unknown) `resolution ≥ 2160` — the 4K
  case the resolution can't let the size hide.
- `run_cast` computes `mirror_ok = needs_remux and mirror.available()` and
  `auto_mirror = mirror_ok and not opts.mirror and _remux_is_pathological(...)`. The mirror runs
  when `mirror_ok and (opts.mirror or auto_mirror)`, so `--mirror`/`cast_mode: "mirror"` still
  force it and the threshold only adds the automatic case. On `auto_mirror` it sets the
  `CastOutcome.notice` (`remux 4K troppo pesante (~N GB) → mirror 1080p …`) and prints it —
  honest surfacing of the 1080p-SDR degrade.
- Below the threshold (a modest 1080p Dolby remux) the remux path is unchanged: native
  video/HDR, `reencoded: true`, no notice.
- Tests: `test_auto_mirror_on_pathological_4k_remux`, `_no_auto_mirror_for_small_1080p_remux`,
  `_auto_mirror_disabled_by_zero_threshold`, `test_remux_pathological_helper`. Existing
  `--mirror` gate tests (`test_mirror_gates_on_remux_audio`, `_downgraded_when_decodable`) still
  pass — the forced path is preserved.

## References

- `src/nstream/cast_flow.py` (`run_cast`, `_remux_is_pathological`), `src/nstream/mirror.py`,
  `src/nstream/remux.py`, `src/nstream/config.py` (`cast_mirror_over_remux_gb`),
  `src/nstream/quality.py` (`cast_remux_max_resolution`, `remux_within_size`).
- ADR 0005 (Tier-2 remux), 0006 (mirror), 0013 (custom receiver — removes the need where the
  device allows), 0014 (fewer accidental remuxes).
