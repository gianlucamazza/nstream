"""Audio-anchored subtitle offset measurement via alass (ADR 0019).

When the subtitle pick is not a protocol hash match, the only strong sync evidence is
the media's own audio. This module extracts a bounded audio segment from the resolved
stream (existing ffmpeg dependency) and lets alass measure the constant offset. The
caller (`subs.auto_subs`) applies it to the full file with `retime_srt`.

Field-falsified TWICE the day it shipped (2026-07-16):
1. Full subtitle vs bounded reference: a huge wrong shift that crams many cues into
   the window beats the true small offset (-514 s observed), plus a spurious fps
   "guess", plus negative-cue clamping in alass's output file.
2. Even window-trimmed, single- AND dense-window measurements are NOISE on real
   content (known +14 s file measured as +0.4/-0.6/-9.0 across three dialogue-dense
   windows). Bounded windows do not give alass enough signal to be trusted alone.

Hence the discipline here:

- the SUB is trimmed to the same window as the audio → bounded alignment space;
- `-g` (no fps guessing) + `-l` (no splits): constant offset only;
- alass's rewritten file is NEVER used — only the measured offset, applied by the
  caller to the intact original;
- measurement runs on THREE dialogue-dense windows and requires CONSENSUS (spread ≤
  `_CONSENSUS_SPREAD_S`): disagreement → None, the caller keeps the uncorrected file
  and its honest `subtitles_match`. On today's evidence this rejects almost always —
  which is the point: no fabricated confidence. The feature ships OFF by default
  (`cfg.sub_autosync`), opt-in experimental until the robust path lands (see the
  'integrate alass logic natively' issue);
- an implausible |offset| (> `max_offset_s`) is rejected the same way.

Best-effort like every optional external tool; the stream url rides in the ffmpeg argv
exactly like it does for mpv/ffprobe — never logged.
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
# Below this many sub cues inside the window the alignment has too little signal.
_MIN_CUES = 10
# Windows measured for consensus, and the max spread (s) between their offsets for the
# measurement to be trusted (field evidence: disagreeing windows = noise, not signal).
_WINDOWS = 3
_CONSENSUS_SPREAD_S = 1.5

# alass reports the applied shift as `by -0:08:34.768` (H:MM:SS.mmm, sign first).
_OFFSET_RE = re.compile(r"by\s+(-?)(\d+):(\d{2}):(\d{2})\.(\d{3})")
# A cue's start timestamp, for the window trim (SRT `,` or `.` separator).
_TS_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def available() -> bool:
    """Whether the audio-anchored measurement can run (alass optdepend + ffmpeg)."""
    return shutil.which("alass") is not None and shutil.which("ffmpeg") is not None


def _parse_offset(output: str) -> float | None:
    """The shift alass reported, in seconds (None when unparsable). `-l` yields a single
    block; if several appear, the largest |shift| is the honest thing to gate on."""
    offsets = []
    for sign, h, m, s, ms in _OFFSET_RE.findall(output):
        v = int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
        offsets.append(-v if sign == "-" else v)
    return max(offsets, key=abs) if offsets else None


def _read_blocks(srt_path: str) -> list[tuple[float, str]]:
    """(start_s, block) per cue of the SRT, or [] on read failure."""
    try:
        with open(srt_path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    out = []
    for block in re.split(r"\n\s*\n", text):
        m = _TS_RE.search(block)
        if m:
            out.append((_ts_s(m), block.strip()))
    return out


def _ts_s(m: re.Match[str]) -> float:
    h, mn, s, ms = m.groups()
    return int(h) * 3600 + int(mn) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _fmt_ts(t: float) -> str:
    t = max(t, 0.0)
    ms = round((t % 1) * 1000)
    w = int(t)
    return f"{w // 3600:02d}:{w % 3600 // 60:02d}:{w % 60:02d},{ms:03d}"


def _dense_windows(starts: list[float], window_s: int) -> list[float]:
    """One dialogue-dense window start per third of the sub's span: alignment needs VAD
    signal, and dialogue density is the best cheap proxy the sub itself offers."""
    total = starts[-1] if starts else 0.0
    thirds: list[float] = []
    k = _WINDOWS
    for i in range(k):
        lo, hi = total * i / k, max(total * (i + 1) / k - window_s, total * i / k)
        cands = [t for t in starts if lo <= t <= hi] or [lo]
        best, best_n = cands[0], -1
        for t in cands[::3]:
            n = sum(1 for x in starts if t <= x <= t + window_s)
            if n > best_n:
                best, best_n = t, n
        thirds.append(max(best - 10.0, 0.0))
    return thirds


def _trim_shifted(blocks: list[tuple[float, str]], out_path: str, t0: float, t1: float) -> int:
    """Write the cues starting in [t0, t1] with timings shifted by -t0 (the audio
    reference's zero is the window start). Returns cues kept."""
    kept = [
        _TS_RE.sub(lambda m: _fmt_ts(_ts_s(m) - t0), block)
        for start, block in blocks
        if t0 <= start <= t1
    ]
    if not kept:
        return 0
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(kept) + "\n")
    except OSError:
        return 0
    return len(kept)


def _measure_window(
    blocks: list[tuple[float, str]], video_url: str, work_dir: str, t0: float, window_s: int
) -> float | None:
    """One window's offset measurement, or None (not enough cues / tool failure)."""
    segment = os.path.join(work_dir, "subsync-window.srt")
    if _trim_shifted(blocks, segment, t0, t0 + window_s) < _MIN_CUES:
        return None
    ref = os.path.join(work_dir, "subsync-ref.wav")
    proc = util.run_cmd(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            str(int(t0)),
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
        if proc is None or proc.returncode != 0 or os.path.getsize(ref) <= 0:
            return None
    except OSError:
        return None
    out = os.path.join(work_dir, "subsync-out.srt")
    proc = util.run_cmd(["alass", "-g", "-l", ref, segment, out], timeout=_ALASS_TIMEOUT)
    if proc is None or proc.returncode != 0:
        return None
    return _parse_offset((proc.stdout or "") + (proc.stderr or ""))


def measure_offset(
    srt_path: str,
    video_url: str,
    work_dir: str,
    *,
    window_s: int = 300,
    max_offset_s: float = 90.0,
) -> float | None:
    """Measure the constant offset (seconds) to add to `srt_path`'s timings so they
    match the real audio of `video_url`, or None when it can't be measured RELIABLY.
    Reliable means: `_WINDOWS` dialogue-dense windows across the runtime each yield an
    offset, they agree within `_CONSENSUS_SPREAD_S`, and the median is plausible
    (≤ `max_offset_s`). Anything less → None: field evidence shows disagreeing windows
    are noise, and a wrong "correction" is worse than an honest guess. This function
    never modifies `srt_path` — the caller applies the offset with `retime_srt`."""
    blocks = _read_blocks(srt_path)
    if len(blocks) < _MIN_CUES * _WINDOWS:
        _log.info("subsync: troppi pochi cue → salto")
        return None
    starts = sorted(b[0] for b in blocks)
    offsets: list[float] = []
    for t0 in _dense_windows(starts, window_s):
        got = _measure_window(blocks, video_url, work_dir, t0, window_s)
        if got is None:
            _log.info("subsync: finestra %.0fs non misurabile → rifiuto", t0)
            return None
        offsets.append(got)
    spread = max(offsets) - min(offsets)
    if spread > _CONSENSUS_SPREAD_S:
        _log.info(
            "subsync: nessun consenso tra finestre (spread %.1fs: %s) → rifiuto",
            spread, [f"{o:+.2f}" for o in offsets],
        )  # fmt: skip
        return None
    median = sorted(offsets)[len(offsets) // 2]
    if abs(median) > max_offset_s:
        _log.info("subsync: offset %.1fs oltre il limite di plausibilità → rifiutato", median)
        return None
    _log.info("subsync: offset misurato %.3fs (consenso di %d finestre)", median, len(offsets))
    return median
