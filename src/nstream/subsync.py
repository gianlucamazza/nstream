"""Audio-anchored subtitle correction via alass (ADR 0019).

When the subtitle pick is not a protocol hash match, the only strong sync evidence is
the media's own audio: metadata can't arbitrate (live case: two timing families 14 s
apart, and the runtime-fit chose the late one because its LAST cue matched the file
duration). This module extracts a bounded audio segment from the resolved stream with
the existing ffmpeg dependency and lets `alass --no-split` measure and apply the
constant offset. `--no-split` is deliberate: with a partial reference, split detection
could mangle cues beyond the window, while a constant offset extrapolates safely.

Best-effort like every optional external tool: any failure returns a "didn't run"
result and the caller keeps today's behaviour (and its honest `subtitles_match`).
The stream url rides in the ffmpeg argv exactly like it does for mpv/ffprobe — never
logged.
"""

from __future__ import annotations

import os
import re
import shutil

from . import log, util

_log = log.get_logger("subsync")

# The audio extraction downloads/decodes only `window_s` seconds of the stream; alass
# then works on a ~14 MB mono 8 kHz WAV. Caps keep a stuck CDN from stalling the cast.
_FFMPEG_TIMEOUT = 90.0
_ALASS_TIMEOUT = 60.0

# alass reports the applied shift on stdout, e.g. "shifted block of 1714 subtitles by
# 750ms" / "by -13.9s". Parsed best-effort for the user notice only.
_OFFSET_RE = re.compile(r"by\s+(-?\d+(?:\.\d+)?)\s*(ms|s)\b")


def available() -> bool:
    """Whether the audio-anchored correction can run (alass optdepend + ffmpeg)."""
    return shutil.which("alass") is not None and shutil.which("ffmpeg") is not None


def _parse_offset(output: str) -> float | None:
    """Largest |shift| alass reported, in seconds, or None when unparsable."""
    offsets = [
        float(v) / 1000 if unit == "ms" else float(v) for v, unit in _OFFSET_RE.findall(output)
    ]
    return max(offsets, key=abs) if offsets else None


def sync_to_audio(
    srt_path: str, video_url: str, work_dir: str, *, window_s: int = 900
) -> tuple[bool, float | None]:
    """Correct `srt_path` IN PLACE against the real audio of `video_url`. Returns
    `(ran, offset_s)`: `ran` False when the correction couldn't run (missing tools,
    extraction/alass failure — caller keeps the uncorrected file and its honest match);
    `offset_s` is the shift alass reported, None when it succeeded but the report line
    wasn't parsable."""
    ref = os.path.join(work_dir, "subsync-ref.wav")
    proc = util.run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-t",
            str(int(window_s)),
            "-i",
            video_url,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "8000",
            ref,
        ],  # fmt: skip
        timeout=_FFMPEG_TIMEOUT,
    )
    try:
        ref_ok = proc is not None and proc.returncode == 0 and os.path.getsize(ref) > 0
    except OSError:
        ref_ok = False
    if not ref_ok:
        _log.info("subsync: estrazione audio fallita (ffmpeg)")
        return False, None
    out = os.path.join(work_dir, "subsync-out.srt")
    proc = util.run_cmd(["alass", "--no-split", ref, srt_path, out], timeout=_ALASS_TIMEOUT)
    if proc is None or proc.returncode != 0:
        _log.info("subsync: alass fallito (rc=%s)", proc.returncode if proc else "n/a")
        return False, None
    try:
        if os.path.getsize(out) <= 0:
            return False, None
    except OSError:
        return False, None
    offset = _parse_offset((proc.stdout or "") + (proc.stderr or ""))
    try:
        shutil.move(out, srt_path)  # in place: downstream (retime/VTT/mpv) is unchanged
    except OSError:
        return False, None
    _log.info("subsync: correzione applicata (offset %s)", offset)
    return True, offset
