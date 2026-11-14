# Verification and recovery

## Automatic gates

Run `bash scripts/check.sh` from any directory. It checks Ruff, formatting, ty, the
offline suite, wheel/sdist contents, and a fresh wheel installation in a disposable venv.
GitHub Actions runs this gate on Python 3.13 and 3.14. A configured workflow is not
evidence that remote CI has run; record the commit and actual CI result at release time.

Tests isolate XDG config/state/cache/runtime directories. Local HTTP and Unix sockets
are permitted; external connections fail. The CLI acceptance harness uses actual HTTP,
subprocesses, ffprobe-shaped JSON, and mpv-shaped Unix IPC. It exercises success,
backend failure, cast handoff/failure/stop, history, argument errors, read-only doctor,
and bounded exit with a stalled addon.
Existing cast policy/backend tests cover direct/remux/mirror and continuation behavior.

`uv run --frozen python scripts/check-playback.py` additionally requires installed
ffmpeg/mpv. It generates a two-second synthetic movie and verifies decoding, IPC,
and natural end, with null video/audio output. It does not disturb the desktop or TV
and does not verify visible frames or audible sound.

## Performance evidence

`uv run --frozen python scripts/benchmark.py --output /tmp/nstream-benchmark.json`
records five samples, median latency, and a ranking fingerprint for 2,000 synthetic
streams and an eight-task offline gather. `--compare <baseline.json>` rejects a different
host/interpreter/ranking fingerprint or a median regression greater than 10%.
Keep the baseline from the previous revision, using the same interpreter and machine.
Do not infer internet playback latency from these microbenchmarks.

Debug logs include named phase timings for search, gather, selection, probe, subtitles,
and observed mpv startup. Timing records contain neither title nor URL.

### Remux and HLS evidence (2026-11-14, ADR 0052)

Local FFmpeg smoke used a generated 12-second 320×180@25 H.264 MKV, two-second
keyframe spacing, E-AC-3 six-channel audio and Italian track metadata. Config, state,
cache and runtime directories were isolated in a temporary directory; no TV was contacted.

| Check | Observed result |
| --- | --- |
| Complete-file `remux_for_cast` + FFmpeg decode to null | Passed: H.264 + AAC, six channels; preparation 0.700 s |
| `live.Producer` + `_live_head_ok` | Passed: 12 s HLS-TS output, valid first segment, AAC stereo |
| Installed catt 0.13.3 `play_media_url`, captured underlying controller call | Passed: HLS MIME, BUFFERED and segment-format metadata forwarded |

The catt controller transport was replaced with a recorder for this experiment. It tests
installed-library argument forwarding, not receiver playback or seeking. The local fixture
timing is not a measurement of remote-source speed or time to first frame on a TV.

Hardware gate remains pending: run the same compatible remote source through HLS and
complete-file delivery, record time to advancing receiver position and ten minutes of
visible/audible stable playback. For the experimental catt sender, additionally verify
start at zero, resume, seeks within and beyond produced segments, subtitles, pause/stop,
and serving after headless return. Keep production HLS castbridge-only until all pass.
At this check, castbridge was unavailable through override, PATH and historical discovery.

## Cast control lifetime

`uv run python scripts/check-cast-lifecycle.py` requires FFmpeg and generates private
synthetic media in a temporary directory. It simulates only the control disconnect;
FFmpeg, HLS generation, the detached HTTP server and the observer's process exit are real.
It verifies playlist/media HTTP 200 after observer exit, cleans up its server and never
contacts a TV. This does not establish physical receiver or network stability (ADR 0056).

## Local diagnostics and recovery

- Run `nstream --doctor` or `nstream --json --doctor` before changing configuration.
  Optional missing binaries are reported independently of required ones. Exit 1 means
  a required check failed. Config permission issues are reported without changing them.
- Playback retains existing JSON files and schemas. There is no data migration.
  Before replacing malformed JSON, an update saves `<name>.json.corrupt-<timestamp>`
  beside it with private permissions. Reads alone do not create recovery files.
- To recover, stop nstream sessions, preserve the current file, inspect the recovery
  copy locally, repair it, and restore the corresponding JSON file with mode 0600.
  Recovery files may contain personal metadata or credentials; never attach them publicly.
- Contended or unwritable state skips the update instead of blocking playback.
  This can lose the latest progress update; it cannot justify an unlocked overwrite.
- For rollback, install the preceding package using the normal installation method.
  State remains compatible. Keep a private backup before any future schema migration.

## Release and hardware acceptance

Before release, verify the version/changelog/Arch metadata, run the automatic gates,
and inspect the actual CI result for the release commit. Publication and host installation
are separate actions; checks never install into the user's system.

On an available receiver, use controlled media to verify direct cast, remux, mirror,
audio language, subtitle visibility/timing, pause/seek/stop, disconnect/recovery, resume,
and next-episode behavior. Check both visible video and audible audio; receiver state
`PLAYING` alone is insufficient. Do not interrupt an unrelated active viewing session.
Record device/backend, media characteristics, result, and any unavailable scenarios.

## Philips recovery acceptance (2026-11-14)

The active live-HLS cast was observed for 900 seconds, with
60 bounded receiver/delivery samples. The operator explicitly authorized
fault injection and confirmed continuous picture and audio, including the fault window.
The test did not issue STOP or LOAD and did not expose media URLs or tokens.

- At 60 seconds, the existing castbridge daemon was suspended for 40 seconds with
  SIGSTOP and restored automatically with SIGCONT. Native telemetry reported
  `disconnected`, then reattached to the same media session (ID 1).
- Receiver state samples: {'PLAYING': 60}. Receiver position advanced
  894.6 seconds. The final native state was PLAYING.
- Every sample found the detached delivery server alive, the EVENT playlist HTTP 200
  and the advertised media segment HEAD HTTP 200; no delivery error was reported.
- Restarting the optional Omarchy shell/plugin during observation did not stop delivery.
- Software exercised: castbridge 0.4.4 and the nstream 1.46/1.47 recovery code (unchanged
  between those versions). The final local installation matches published nstream 1.47.0
  and plugin 0.4.0. Launcher regression gates: 1947 Python tests and 27 plugin tests,
  including Qt QML action execution and real history/watch contracts against PyPI.

This is empirical evidence for this receiver/session, not a guarantee for every receiver
or source. The fault tests control-path interruption, not host/network loss of HLS media.
See ADRs 0056, 0058 and 0059.


## Transfer and presence checks — 1.48.0 / plugin 0.5.0 (2026-11-15)

The full gate passed 1,996 tests, Ruff, types and wheel/sdist installation checks.
Local-only tests exercise exact release filtering, pause/cursor alignment, rollback,
physical receiver replacement, identity-checked legacy presence cleanup, daemon terminal
binding and HLS reload rebinding. Torrent hash alone does not identify a file in a pack.

Real mpv tests use generated WAV media with null audio/video. They verify presence without
history, pause, seek, quit and cleanup. A separate process test verifies readiness ACK,
exact episode metadata, continued worker lifetime after ACK and player cleanup on SIGTERM.
The same local lifecycle smoke passed against the installed package.

The optional plugin passed all 29 helper/contract/QML tests against that installed core.
Its installed idle panel was visually checked: no active TV title or transport controls;
continue-watching remains available. Neither this check nor the local player smokes sent
STOP or LOAD to the Philips.

A complete physical TV ↔ computer transfer, including visible frames/audible audio and
TV-specific seek accuracy, remains an acceptance check. The earlier Philips reconnect
acceptance does not certify this new transfer transaction.
