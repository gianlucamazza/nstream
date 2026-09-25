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
