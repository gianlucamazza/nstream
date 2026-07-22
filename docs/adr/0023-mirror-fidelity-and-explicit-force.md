# 0023. Mirror fidelity (HDR→SDR) and an explicit `--mirror` that forces

- **Status:** Accepted
- **Date:** 2026-07-22
- **Deciders:** project maintainer

## Context

The realtime mirror (ADR 0006) decodes a stream in mpv on a Hyprland headless Wayland output and
the openscreen sender H.264-encodes that surface to the TV. It is the last-resort backend and, per
ADR 0015, the auto-preferred one for a pathological 4K rewrap — including the 4K-mkv rewrap that
ADR 0022 now routes here. Two problems surfaced while diagnosing the ADR-0022 incident:

1. **No HDR tone-mapping.** Neither `mirror._mpv_args` nor any `player._*_defaults` helper applied
   any HDR→SDR conversion (`--target-prim`/`--target-trc`/`--tone-mapping`). mpv renders an HDR
   (BT.2020/PQ) source onto the 8-bit SDR surface with no tone-map; the sender captures and encodes
   it as out-of-range. Observed on a 4K HDR cast: audio (on the independent PipeWire null sink)
   played while the TV showed **black**.
2. **`--mirror` was silently overridden.** The backend gate was
   `mirror_ok = (needs_remux or force_mirror) and mirror.available()` — `opts.mirror is True` was
   never OR-ed in, only consulted _inside_ the already-gated branch. So an explicit `--mirror` (or
   `cast_mode: "mirror"`) on a DMR-decodable title printed a "mirror not needed" notice and cast
   directly. The user could not force the mirror for a clean title.

## Decision

1. **Pin an SDR target in the mirror mpv** (`mirror._TONEMAP_ARGS`:
   `--target-prim=bt.709 --target-trc=bt.1886 --tone-mapping=bt.2390`), injected before
   `cfg.mpv_args` so a user override still wins. mpv's GPU renderer then tone-maps HDR→SDR onto the
   captured surface. A no-op for SDR sources.
2. **Honor an explicit `--mirror`**: `mirror_ok` gains `or opts.mirror is True`, so a forced mirror
   is chosen even for decodable audio + good video — the manual intent wins, mirroring how
   `--no-mirror` (ADR 0021) suppresses the mirror. It only downgrades to a direct cast when the
   mirror backend is unavailable (`MIRROR_UNAVAILABLE` notice).

## Consequences

- A 4K HDR mirror (including the ADR-0022 4K-mkv fallback) renders in SDR instead of black.
- `--mirror` / `cast_mode: "mirror"` is now an override, not a hint. This changes the practical
  behavior described in ADR 0006 ("decodable audio transparently downgrades to a direct cast")
  without mutating that ADR's decision — the downgrade now happens only when the mirror is
  unavailable.
- The tone-map is unconditional (SDR-safe); a user who wants HDR passthrough on a future
  HDR-capable mirror path can re-enable it via `cfg.mpv_args`.
