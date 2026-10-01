"""Sparse REMOTE probing for the alignment engine — Phase-0 bench only (ADR 0020).

Probes N short windows of a remote stream within a byte budget and assembles the same
`subalign.Fingerprint` the local path builds. Phase 0 measured its real cost at
100-300 MB per cast, so production aligns only against a LOCAL file
(`subalign.probe_local`, the Tier-2 remux); this module is kept for the bench
(`python -m nstream.subalign`, see `_bench.py`) and is imported by nothing else.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import log, subalign, urlproxy, util
from .subalign import (
    _EDGE_S,
    _FFMPEG_TIMEOUT,
    _INFORMATIVE,
    _SPEECH_FILTER,
    _WARMUP_S,
    Fingerprint,
    Span,
    _to_abs,
)

_log = log.get_logger("subalign")

_BUDGET_BYTES = 25_000_000  # transfer cap per cast (issue #2); internal, not config
_PROBE_OVERHEAD_B = 600_000  # per-invocation container overhead (Phase-0 calibrated)
_N_TARGET = 10  # desired probe count; degrades to _N_MIN on high byte-rate files
_N_MIN = 6
_D_MIN_S, _D_MAX_S = 4.0, 30.0  # per-probe duration bounds
_USABLE = (0.03, 0.97)  # skip intro/credits fractions (music-heavy)
_MAX_WORKERS = 4

_SIL_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SIL_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def plan_probes(
    duration: float, size_bytes: int, cue_starts: list[float], *, budget_bytes: int = _BUDGET_BYTES
) -> list[Span] | str:
    """Plan N dialogue-dense probe windows within the byte budget, or a refusal reason.
    Cost per probed second = container byte-rate (interleaved media), NOT audio bitrate."""
    if duration <= 0 or size_bytes <= 0:
        return "no_media_geometry"
    bps = size_bytes / duration
    n = _N_TARGET
    d = 0.0
    while n >= _N_MIN:
        d = (budget_bytes - n * _PROBE_OVERHEAD_B) / bps / n
        if d >= _D_MIN_S:
            break
        n -= 2
    else:
        return "bitrate_over_budget"
    d = min(d, _D_MAX_S)
    lo, hi = duration * _USABLE[0], duration * _USABLE[1] - d
    if hi <= lo:
        return "no_media_geometry"
    starts = sorted(t for t in cue_starts if lo <= t <= hi)
    windows: list[Span] = []
    for i in range(n):
        s_lo = lo + (hi - lo) * i / n
        s_hi = lo + (hi - lo) * (i + 1) / n
        cands = [t for t in starts if s_lo <= t <= s_hi] or [s_lo]
        best, best_n = cands[0], -1
        for t in cands[::3]:
            k = sum(1 for x in starts if t <= x <= t + d)
            if k > best_n:
                best, best_n = t, k
        t0 = max(best - 10.0, lo)
        windows.append((t0, t0 + d))
    return windows


def _silencedetect_spans(url: str, t0: float, d: float) -> list[Span] | None:
    """Fallback extractor (ametadata format drift across ffmpeg versions): speech spans
    from inverted silencedetect intervals, window-relative."""
    proc = util.run_cmd(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-v",
            "info",
            "-ss",
            f"{t0:.3f}",
            "-t",
            f"{d:.3f}",
            "-i",
            url,
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            f"{_SPEECH_FILTER},silencedetect=n=-35dB:d=0.35",
            "-f",
            "null",
            "-",
        ],  # fmt: skip
        timeout=_FFMPEG_TIMEOUT,
    )
    if proc is None or proc.returncode != 0:
        return None
    err = proc.stderr or ""
    starts = [float(x) for x in _SIL_START_RE.findall(err)]
    ends = [float(x) for x in _SIL_END_RE.findall(err)]
    # invert silences into speech within [0, d]
    speech: list[Span] = []
    cur = 0.0
    for s, e in zip(sorted(starts), sorted(ends), strict=False):
        if s > cur:
            speech.append((cur, s))
        cur = max(cur, e)
    if cur < d:
        speech.append((cur, d))
    return speech


def _extract_window(url: str, w: Span) -> tuple[Span, list[Span]] | None:
    """One probe: effective window + absolute speech spans, or None (failed /
    uninformative). Primary extractor = RMS series + adaptive VAD; fallback =
    silencedetect at a fixed threshold."""
    t0, t1 = w
    d = t1 - t0
    series = subalign._rms_series(url, t0, d)
    if series is not None:
        series = [(t, v) for t, v in series if t >= _WARMUP_S]
        if not series:
            return None
        rel_spans = subalign._spans_from_rms(series)
    else:
        rel_spans = _silencedetect_spans(url, t0, d)
        if rel_spans is None:
            return None
        rel_spans = [(max(s, _WARMUP_S), e) for s, e in rel_spans if e > _WARMUP_S]
    eff: Span = (t0 + _WARMUP_S + _EDGE_S, t1 - _EDGE_S)
    if eff[1] <= eff[0]:
        return None
    spans_abs = [
        (max(_to_abs(t0, s), eff[0]), min(_to_abs(t0, e), eff[1]))
        for s, e in rel_spans
        if _to_abs(t0, e) > eff[0] and _to_abs(t0, s) < eff[1]
    ]
    speech_s = sum(e - s for s, e in spans_abs)
    frac = speech_s / (eff[1] - eff[0])
    if not (_INFORMATIVE[0] <= frac <= _INFORMATIVE[1]):
        return None  # wall-to-wall chatter/music or no dialogue: noise, not evidence
    return eff, spans_abs


def probe(
    video_url: str,
    duration: float,
    size_bytes: int,
    *,
    budget_s: float = 30.0,
    budget_bytes: int = _BUDGET_BYTES,
    cue_starts: list[float] | None = None,
) -> subalign.Fingerprint | str:
    """Extract the media's sparse speech fingerprint, or a refusal reason. Network is
    paid HERE, once per cast; `align` is then pure per candidate."""
    # NB: the loopback (P2P gateway) refusal belongs to the ORCHESTRATOR's tier-2 gate,
    # not here — the Phase-0 bench legitimately probes through a local counting proxy.
    if not subalign.available():
        return "no_ffmpeg"
    plan = plan_probes(duration, size_bytes, cue_starts or [], budget_bytes=budget_bytes)
    if isinstance(plan, str):
        return plan
    deadline = time.monotonic() + max(budget_s - 5.0, 5.0)
    video_url = urlproxy.local_url(video_url)  # the ffmpeg argv never sees a debrid token
    results: list[tuple[Span, list[Span]]] = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        futs = [ex.submit(_extract_window, video_url, w) for w in plan]
        for f in futs:
            left = deadline - time.monotonic()
            if left <= 0:
                ex.shutdown(wait=False, cancel_futures=True)
                return "deadline_exceeded"
            try:
                got = f.result(timeout=left)
            except TimeoutError:
                ex.shutdown(wait=False, cancel_futures=True)
                return "deadline_exceeded"
            if got is not None:
                results.append(got)
    min_ok = max(4, len(plan) - 2)
    if len(results) < min_ok:
        reason = "low_speech" if results else "probe_failures"
        _log.info("subalign: %d/%d finestre utili → %s", len(results), len(plan), reason)
        return reason
    results.sort(key=lambda r: r[0][0])
    windows = tuple(w for w, _ in results)
    speech = tuple(s for _, spans in results for s in spans)
    return Fingerprint(duration=duration, windows=windows, speech=speech)
