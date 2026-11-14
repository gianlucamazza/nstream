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
