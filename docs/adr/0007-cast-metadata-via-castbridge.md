# 0007. Native cast delivery: castbridge sender + nstream Range server (metadata + events)

- **Status:** Accepted
- **Date:** 2026-06-05
- **Deciders:** project maintainer
- **Amended by:** [0050](0050-catt-fallback-load-metadata.md) — catt 0.13 *can* send
  title + `streamType` via `-l` / `--stream-type`. The “metadata-less LOAD” claim
  below is the 0.13.1 reading without those flags; poster / Movie / TvShow still
  need castbridge (no `--thumb` on the CLI).
- **Supersedes:** the *delivery mechanism* of ADR 0005 (catt as Cast sender + file server);
  ADR 0005's findings on the DMR (HEVC/4K/HDR native, no Dolby passthrough, complete-file +
  Range-served requirement, audio-track selection needs remux) all still hold.
- **Implemented in:** `src/nstream/bridge.py`, `src/nstream/serve.py` (new),
  `src/nstream/caster.py`, `src/nstream/remux.py`, `src/nstream/cli.py`; C++ side in
  `~/Workspace/tooling/cast/native/castbridge/`.

## Context

nstream casts via `catt` (v0.13.1). `catt cast` sends a metadata-less LOAD to the Default
Media Receiver (DMR), so:

- The TV's now-playing screen shows a generic UI — no title, no poster.
- The JARVIS HUD listener (`~/.config/eww/scripts/cast-listen.py`, a protocol-level
  pychromecast bridge already feeding the `cast-indicator` widget + ticker toasts +
  `jarvis-power/cast.state`) reads `media_controller.status.title` → empty → falls back to a
  generic name. The widget exists but stays bare. **Same root cause as the TV.**
- Headless tracking is fire-and-return (`caster.cast(follow=False)` returns position 0); the
  only state read is a lazy `--status` poll. No started/playing/ended event stream for agents
  or the nstream skill.

`catt` cannot send media metadata and exposes no event stream. We already own a native Cast
stack: `castbridge` (C++, built on a forked openscreen) is *"the native equivalent of what
catt did"* — connect → LAUNCH → LOAD on `CC1AD845`, play/pause/seek/stop, and it **already
emits** `media-status` (live position) and `session`/`session-ended` events over its AF_UNIX
IPC.

## Decision

Make **castbridge the single Cast-protocol sender** for nstream casting, carrying full media
metadata, and source the now-playing state from its IPC event stream. Keep responsibilities
cleanly split — **castbridge owns the Cast protocol; the file's owner owns content delivery**:

1. **Metadata (C++).** Extend `castbridge` `LoadRequest` + `SendLoad()` to emit
   `metadataType=1` (Movie: title, subtitle, `images:[poster]`) or `=2` (TvShow:
   seriesTitle, season, episode, title, `images:[poster]`); extend the `media-load` IPC args
   accordingly. Poster is a public Cinemeta HTTPS URL the DMR fetches.

2. **Drive model.** nstream speaks castbridge's AF_UNIX IPC directly (stdlib `socket`+`json`,
   no new dependency) via a new leaf module `bridge.py`: send `media-load`, consume
   `media-status`/`session` events. nstream spawns `castbridge --daemon` (reusing the
   existing `flock` spawn guard) if not already running; the daemon is shared with the
   LibreWolf extension (single instance per `$XDG_RUNTIME_DIR`).

3. **Content delivery, both tiers, one owner each.**
   - Tier-1 (audio already DMR-decodable): castbridge LOADs the remote debrid/torrserver URL
     directly — as catt did, plus metadata.
   - Tier-2 (Dolby/DTS → on-host remux to a complete AAC MP4): **nstream serves the complete
     file itself** via a stdlib **Range HTTP server** (`serve.py`: 206 + `Content-Range` +
     `Content-Length` + `Accept-Ranges`, HEAD supported), and castbridge LOADs that
     `http://<lan-ip>:<port>/<file>` URL with metadata. This replaces the detached catt file
     server of ADR 0005. The server lives where the file lives (remux.py owns the temp file
     lifecycle + GC), keeping castbridge a pure protocol client.

4. **Events → `--follow` JSONL.** `--json --cast --follow` emits one JSON line per transition
   (started/playing/paused/ended/failed) derived from castbridge's IPC events — event-sourced,
   not polled. Without `--follow`: fire-and-return (one summary line).

5. **catt = clean full-stack fallback.** When the castbridge binary isn't built, nstream
   degrades to the existing catt path for **both** tiers (catt sends+serves, no metadata) —
   the same graceful-degradation pattern as catt→mpv. **No mixed castbridge+catt session.**

## Rationale

- **No workaround.** The rejected interim idea (catt serves the Tier-2 file while castbridge
  sends a second LOAD at catt's URL) means two senders on one receiver and a "metadata absent
  if not practicable" escape hatch — a cerotto. A stdlib Range server is the *correct*
  implementation of exactly what catt's server does (catt itself is a Python Range server),
  so owning it removes the dual-session hazard and guarantees metadata on both tiers.
- **Single root, single fix.** Sending real metadata lights up the TV card *and* the existing
  HUD widget at once — no HUD changes, because `cast-listen.py` is protocol-level.
- **Zero new runtime deps.** `bridge.py`/`serve.py` are pure stdlib; nstream's "orchestrate
  external binaries, zero Python deps" core is preserved. castbridge is an external binary
  like catt/mpv/ffprobe, located at runtime (env override).
- **Event-sourced tracking.** castbridge already pushes the exact transitions we need; reading
  them beats re-polling `catt info -j`.

| Alternative | Verdict |
|-------------|---------|
| **castbridge sender + nstream Range server (chosen)** | Native, metadata both tiers, real events, no dual session. Cost: castbridge needs the openscreen-fork build. |
| External pychromecast helper (catt venv) | Works but adds a second Cast stack to maintain alongside castbridge; pychromecast metadata support is thinner than a direct LOAD. |
| Patch/keep catt for metadata | Impossible: catt v0.13.1 has no metadata LOAD and no event stream. |
| Tier-2 = catt serves + castbridge LOADs its URL | Rejected workaround: two senders/one receiver, metadata-gap escape hatch. |

## Consequences

- **Gain:** title + poster on the TV now-playing card and the HUD widget; a real
  started→playing→ended JSONL stream for the nstream skill and JARVIS agents; one native Cast
  path instead of shelling to catt.
- **Cost — build:** castbridge requires the one-time (~GB, slow) openscreen-fork build
  (`cast/native/integration/build.sh`). It is therefore the *preferred* path with catt as
  fallback, not a hard requirement — document in the nstream README.
- **New modules + lifecycle:** `bridge.py` (IPC client, daemon spawn guard) and `serve.py`
  (Range server + `python -m nstream.serve` entrypoint, detached for headless, reusing
  remux's state file + GC for teardown).
- **`serve.py` binds the firewall-allowed cast port range (45000-47000), not an ephemeral
  port.** Field validation surfaced this: the receiver connects *back* to the host to fetch the
  Tier-2 file, and the host's default-deny UFW dropped a random ephemeral port (`cast_startup_failed`
  with no `media-status`). That range is **catt's own** (`catt/stream_info.py: random.randrange(
  45000,47000)`) — nstream replaces catt's server, so sharing the band is principled, not luck.
  `serve.py` binds inside it (falling back to ephemeral, logged, only if the range is busy).
- **`serve.py` auto-ensures the ufw rule before a Tier-2 serve** (`ensure_firewall`, called from
  `remux.cast_file`, covering both the castbridge-serve and catt-serve paths). It mirrors
  skill-cast's `cast-screen fw-setup` idiom with a **byte-identical** rule (`sudo -n ufw allow
  from <subnet/24> to any port 45000:47000 proto tcp`), so nstream/catt/skill-cast share **one**
  rule (ufw dedups). Idempotent (grep `ufw status` first), LAN-scoped, persistent (no teardown,
  like skill-cast). **Best-effort, never raising:** a no-op when ufw isn't installed, sudo isn't
  passwordless, or ufw is inactive (other firewalls / non-Linux) — there casting relies on a
  pre-existing rule, and a Tier-2 startup failure prints `firewall_hint` (the exact rule to add)
  so the block isn't opaque. Scope is Tier-2 only: Tier-1 (remote URL) and the daemon cast channel
  are outbound, needing no rule, so nstream stays unprivileged for ordinary casts.
- **Field validation (ADR 0005 discipline) — DONE (2026-06-05, Philips 43PUS9235):** the rich LOAD
  is accepted (the receiver echoes `media_metadata: {metadataType:1, title, subtitle, images:[poster]}`)
  and the stdlib Range server plays through end-to-end (`started → playing 0→27.4 → ended`, the
  receiver reports `PLAYING` + the metadata). The UFW port-range fix above was the one real
  environmental blocker found and resolved.
- **C++ scope:** `media_receiver_client.{h,cc}` (LoadRequest + SendLoad) and `daemon.cc`
  (media-load args) in the cast repo; rebuild + update its `castbridge/README.md` IPC table.

## References

- ADR 0005 (`docs/adr/0005-cast-delivery-tier2-remux.md`) — DMR findings retained.
- castbridge: `~/Workspace/tooling/cast/native/castbridge/` (`media_receiver_client.*`,
  `daemon.cc`, `ipc_server.*`, `README.md`); `cast/CLAUDE.md`.
- HUD bridge: `~/.config/eww/scripts/cast-listen.py`.
- Google Cast media metadata: <https://developers.google.com/cast/docs/media>.
