# 0056. Control loss is not playback completion

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** maintainer
- **Amends:** 0031, 0039, 0044 (terminal lifecycle and HLS ownership)

## Context

A Philips cast started at 21:22:38. At 21:26:17 castbridge logged a TLS socket failure
while the daemon stayed alive. Native `MediaController::OnClosed` erased its active
snapshot and emitted an idle session. `bridge.cast_load` mapped this to `ended`, and
the followed HLS path could tear down HTTP and FFmpeg under the receiver.

## Decision

Native media status gains `endReason`: finished, stopped, interrupted or disconnected.
Terminal events preserve the last position/duration. Transport destruction does not
implicitly STOP media. Explicit stop and intentional replacement may stop verified media.
Observers treat unclassified legacy idle events as disconnected, not finished.

Native control recovery makes three attempts after 1/2/4 seconds, each with a 12-second
deadline. It connects to the existing receiver app with GET_STATUS instead of LAUNCH,
then queries MEDIA_STATUS instead of LOAD. App session, media session and content ID
must match the original request before recovery succeeds. Recovery never rewinds, changes
tracks or modifies another media session. Failed attempts close transport without STOP.
Epoch guards cancel stale retries/timeouts after stop or replacement. Weak callbacks
prevent a queued connect from using a destroyed client.

HLS always uses detached `serve` ownership, even when the TUI follows its events. Confirmed
completion, stop/replacement, delivery error or idle expiry reclaims resources. Loss of
control ends observation but preserves the serving PID/state/media. Bridge terminal reasons
are carried in internal results; only finished can auto-advance, with the usual position
threshold. Other backends retain their existing completion rules.

## Consequences

- A dead control channel no longer fabricates a playback end or removes HLS media.
- The TV can continue fetching while control reconnects; a real data-network outage may
  still interrupt playback. The original cause of the field TLS failure is unproven.
- Native protocol changes are additive. Old-daemon ambiguity keeps media alive until
  explicit cleanup/idle expiry, at the cost of no automatic advancement for that event.
- JSONL terminal events can include end_reason; URLs/tokens remain private.
- `scripts/check-cast-lifecycle.py` verifies real HLS serving after simulated control loss
  and the observer process's exit, without contacting a receiver.
- Physical-TV long-runtime/reconnect verification remains a separate acceptance gate.

## Verification

The Python release gate passed 1,896 offline tests plus lint, types and package/install
checks. The companion native build passed 11 IPC/mirror tests and 14 controller/identity
unit tests, including stale closure, stop cancellation and queued-connect destruction.
The real-process HLS smoke also passed using the installed package after local deployment.
These checks do not establish fifteen-minute audiovisual stability on the physical TV.

## References

- `cast/native/castbridge/media_controller.cc`, `media_receiver_client.cc`,
  `cast_channel_client.cc`, `daemon.cc` (companion cast repository).
- `bridge.cast_load`, `cast_delivery.drive_bridge`, `remux.cast_live`, `cast_flow.run_cast`.
