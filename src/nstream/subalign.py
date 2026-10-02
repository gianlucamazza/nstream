"""Native subtitle alignment engine (ADR 0020).

When the subtitle pick is not a protocol hash match, the only strong sync evidence is
the media's own audio. `probe_local` decodes the audio of a LOCAL media file (the Tier-2
remux) into speech spans with an adaptive threshold, split into virtual windows; `align`
finds the constant offset that maximizes a coverage-normalized overlap between the
subtitle cues and the detected speech. The concept is that of a no-split interval
alignment (as in subtitle-sync literature); the implementation is original, pure
Python, stdlib-only — ffmpeg is the sole signal extractor. The sparse REMOTE probing of
Phase 0 lives in `_subalign_remote` (bench only: 100-300 MB per cast).

Design doctrine (paid for in the field, ADR 0019 post-scriptum):
- **probe/align split**: `probe_local()` pays the decode ONCE per cast; `align()` is pure
  math, run once per candidate — and testable on recorded fixtures with zero I/O.
- **coverage-normalized score** `overlap(S+δ, speech) / overlap(S+δ, windows)`: a δ
  that slides cues out of the probed regions shrinks numerator and denominator
  together, killing the runaway-shift failure mode of naive overlap.
- **multi-gate verdict, no fabricated confidence scalar**: acceptance is
  `reason == "aligned"`; every refusal carries a typed reason and the diagnostics.
- **measure-then-apply**: this module never touches subtitle files; the caller applies
  `srt.retime(path, verdict.offset_s, 1.0)` to the intact original.
- fps/drift correction is deliberately OUT of v1 (no ground truth to gate it); the
  drift diagnostic refuses honestly (`drift_suspected`) instead of delivering a wrong
  constant. A v2 pass would refit with scale ∈ {25/23.976, ...} against its own gate.

All engine thresholds are internal (fixture-calibrated in Phase 0, see
`tests/data/coherence.json`), NOT config: a user knob on a statistical gate invites
fabricated confidence; the user lever stays `--sub-offset`.
"""

from __future__ import annotations

import re
import shutil
import urllib.parse
from dataclasses import dataclass

from . import log, util

_log = log.get_logger("subalign")

Span = tuple[float, float]  # [start, end) seconds, absolute media time

# --- ffmpeg ---------------------------------------------------------------------
_FFMPEG_TIMEOUT = 20.0
_FFMPEG_LOCAL_TIMEOUT = 180.0  # one full-file decode pass (local IO, CPU-bound)

# --- signal ---------------------------------------------------------------------
_WARMUP_S = 0.3  # decoder ramp discarded at window head
_EDGE_S = 0.4  # effective-window margin on both sides
_VAD_FLOOR_PCT = 15  # per-window noise floor percentile
_VAD_ENTER_DB = 9.0  # floor + this → speech
_VAD_HYST_DB = 4.0  # exit threshold = enter − this
_VAD_MERGE_S = 0.20
_VAD_MIN_SPAN_S = 0.30
_INFORMATIVE = (0.05, 0.90)  # windows outside this speech-fraction are excluded

# --- alignment ------------------------------------------------------------------
_SEARCH_S = 120.0  # |δ| search bound
_STEPS = (0.25, 0.05, 0.01)  # hierarchical grid
_MIN_DEN_S = 8.0  # anti-degenerate coverage floor per score evaluation (full fingerprint)
_MIN_DEN_ONE_S = 2.0  # per-window floor (a single window holds only a few sub seconds)
_VOTE_TOL_S = 0.75  # peak clustering tolerance for the cross-window vote
_VOTE_PEAKS_K = 6  # peaks each window may nominate

# --- verdict gates (initial values; Phase-0 calibrated with ≥30% margin) ----------
_MIN_SCORE = 0.50
_PEAK_DELTA = 0.08
_PEAK_RATIO = 1.12
_EXCLUDE_S = 1.0  # runner-up exclusion zone around the peak
_MIN_COVERAGE_S = 12.0
_MIN_CUES = 15
_MAX_SPLIT_S = 0.7
_MAX_DRIFT_TOTAL_S = 2.0
_MAX_OFFSET_S = 120.0

_REASONS = frozenset(
    {
        "aligned",
        "p2p_source",
        "no_ffmpeg",
        "no_media_geometry",
        "bitrate_over_budget",
        "probe_failures",
        "low_speech",
        "no_cues",
        "low_coverage",
        "low_score",
        "ambiguous_peak",
        "cross_window_disagree",
        "split_half_disagree",
        "drift_suspected",
        "implausible_offset",
        "deadline_exceeded",
    }
)


@dataclass(frozen=True)
class Fingerprint:
    """Sparse speech evidence of the MEDIA: N short probed windows across the whole
    runtime, and the speech spans detected inside them. Absolute media time."""

    duration: float
    windows: tuple[Span, ...]  # effective (margin-shrunk) informative windows
    speech: tuple[Span, ...]  # detected speech spans, clipped to the windows


@dataclass(frozen=True)
class Alignment:
    """Diagnostics of one candidate's alignment landscape (kept even on refusal)."""

    offset: float  # argmax δ (to ADD to sub times; late subs → negative)
    score: float  # coverage-normalized precision at the peak
    runner_up: float  # best score outside ±_EXCLUDE_S of the peak
    coverage_s: float  # sub seconds inside the windows at the peak
    cues_in_window: int
    split_delta: float  # |δ(even windows) − δ(odd windows)|
    drift_total_s: float  # |per-window residual slope| × duration
    votes: int  # windows whose own peak list supports the winning δ
    windows_n: int  # informative windows available for the vote


@dataclass(frozen=True)
class Verdict:
    """Outcome for one candidate. Accepted ⇔ reason == "aligned" (offset_s set)."""

    offset_s: float | None
    scale: float  # always 1.0 in v1 (constant-offset pass only)
    reason: str
    diag: Alignment | None


def available() -> bool:
    """Whether the engine can run (ffmpeg is its only external tool)."""
    return shutil.which("ffmpeg") is not None


def is_loopback(url: str) -> bool:
    """The local P2P gateway: sparse `-ss` probes there force scattered torrent pieces
    at playback start — the piece-deadline anti-pattern. The caller refuses tier 2."""
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in ("127.0.0.1", "::1", "localhost")


# --- signal extraction ---------------------------------------------------------------

_RMS_PTS_RE = re.compile(r"pts_time:\s*([0-9.]+)")
_RMS_VAL_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[0-9.]+|-inf)")

_SPEECH_FILTER = "highpass=f=120,lowpass=f=4000"


def _to_abs(t0: float, t_rel: float) -> float:
    """Window-relative → absolute media time. `-ss` before `-i` is an accurate input
    seek that rebases output pts near 0 — asserted and validated by the Phase-0 anchor
    gate (G1); a surprise there is fixed HERE, in one place."""
    return t0 + t_rel


def _mono_chain(channels: int | None) -> str:
    """Downmix to one 16 kHz channel before measuring: the dialogue lives in the centre
    channel of a 5.1/7.1 mix (and is cleanest there), any other layout is averaged. The
    RMS only needs speech activity, and per-channel stats on a 6-channel 48 kHz stream
    made a full-film pass ~8x slower (measured 19.1 s → 2.4 s per 600 s of 5.1 audio) —
    slow enough that every local alignment hit its timeout (2026-10-01)."""
    first = "pan=mono|c0=FC" if channels and channels >= 6 else "aformat=channel_layouts=mono"
    return f"{first},aresample=16000"


def _rms_series(
    url: str,
    t0: float,
    d: float,
    *,
    timeout: float = _FFMPEG_TIMEOUT,
    channels: int | None = None,
) -> list[tuple[float, float]] | None:
    """(t_rel, rms_db) at 100 ms resolution for the window, or None on failure."""
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
            # asetnsamples makes 100 ms frames; astats reset=1 → per-frame RMS. `reset`
            # takes INTEGER frames: a fractional value silently disables the reset and
            # the cumulative RMS flattens all contrast (found live in Phase 0).
            f"{rms_filter(channels)},ametadata=mode=print:key=lavfi.astats.Overall.RMS_level",
            "-f",
            "null",
            "-",
        ],  # fmt: skip
        timeout=timeout,
    )
    if proc is None or proc.returncode != 0:
        return None
    return parse_rms(proc.stderr or "") or None


def parse_rms(text: str) -> list[tuple[float, float]]:
    """(pts_time, rms_db) pairs from ffmpeg's `ametadata=mode=print` output — stderr of
    `_rms_series`, or the `file=` the live producer writes (ADR 0040 point 2)."""
    out: list[tuple[float, float]] = []
    pts: float | None = None
    for ln in text.splitlines():
        m = _RMS_PTS_RE.search(ln)
        if m:
            pts = float(m.group(1))
            continue
        v = _RMS_VAL_RE.search(ln)
        if v and pts is not None:
            out.append((pts, -100.0 if v.group(1) == "-inf" else float(v.group(1))))
            pts = None
    return out


def rms_filter(channels: int | None) -> str:
    """The speech-activity filter chain (`-af`): mono 16 kHz, speech band, 100 ms frames,
    per-frame RMS. Shared by `_rms_series` and the live producer's tee."""
    return (
        f"{_mono_chain(channels)},{_SPEECH_FILTER},asetnsamples=n=1600,"
        "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level"
    )


def fingerprint_from_series(
    series: list[tuple[float, float]], t0: float, t1: float, *, segments: int = 8
) -> Fingerprint | str:
    """Fingerprint over [t0, t1) of an RMS series, split into `segments` virtual windows
    (see `probe_local`). Times stay absolute (film time when the series is)."""
    if t1 - t0 <= 60 or segments < 4:
        return "no_media_geometry"
    seg_len = (t1 - t0) / segments
    windows: list[Span] = []
    speech: list[Span] = []
    for i in range(segments):
        a, b = t0 + i * seg_len, t0 + (i + 1) * seg_len
        chunk = [(t, v) for t, v in series if a <= t < b]
        if len(chunk) < 50:
            continue
        spans = _spans_from_rms(chunk)
        eff: Span = (a + (_WARMUP_S if i == 0 else 0.0) + _EDGE_S, b - _EDGE_S)
        clipped = [(max(s, eff[0]), min(e, eff[1])) for s, e in spans if e > eff[0] and s < eff[1]]
        frac = sum(e - s for s, e in clipped) / (eff[1] - eff[0])
        if not (_INFORMATIVE[0] <= frac <= _INFORMATIVE[1]):
            continue
        windows.append(eff)
        speech.extend(clipped)
    if len(windows) < max(4, segments - 2):
        _log.info("subalign: %d/%d segmenti utili → low_speech", len(windows), segments)
        return "low_speech"
    return Fingerprint(duration=t1, windows=tuple(windows), speech=tuple(speech))


def _spans_from_rms(series: list[tuple[float, float]]) -> list[Span]:
    """Hysteresis VAD over the RMS series with a PER-WINDOW adaptive threshold: the
    noise floor is this window's low percentile, so a loud scene and a quiet one get
    comparable treatment. Normalizers (dynaudnorm & co.) are deliberately absent —
    they lift the floor toward speech, destroying the contrast a threshold needs."""
    vals = sorted(v for _, v in series)
    floor = vals[max(0, int(len(vals) * _VAD_FLOOR_PCT / 100) - 1)]
    enter = max(floor + _VAD_ENTER_DB, -55.0)
    leave = enter - _VAD_HYST_DB
    spans: list[Span] = []
    start: float | None = None
    for t, v in series:
        if start is None and v >= enter:
            start = t
        elif start is not None and v < leave:
            spans.append((start, t))
            start = None
    if start is not None:
        spans.append((start, series[-1][0] + 0.1))
    # merge close gaps, drop shards
    merged: list[Span] = []
    for s, e in spans:
        if merged and s - merged[-1][1] < _VAD_MERGE_S:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e - s >= _VAD_MIN_SPAN_S]


def probe_local(
    path: str,
    *,
    duration: float,
    segments: int = 8,
    timeout_s: float = _FFMPEG_LOCAL_TIMEOUT,
    channels: int | None = None,
) -> Fingerprint | str:
    """Full-signal fingerprint from a LOCAL media file (the Tier-2 remux cast: the whole
    file is already on disk, so the audio evidence is free — no network, only one ffmpeg
    decode pass). The full RMS series is split into `segments` VIRTUAL windows so the
    cross-window vote, split-half and drift gates keep their power: each segment holds
    minutes of speech instead of seconds. This is the regime where interval alignment is
    proven; the sparse remote mode remains bench-only (Phase 0 measured its budget at
    100-300 MB/cast — see ADR 0020)."""
    if not available():
        return "no_ffmpeg"
    if duration <= 60 or segments < 4:
        return "no_media_geometry"
    series = _rms_series(path, 0.0, duration, timeout=timeout_s, channels=channels)
    if not series:
        return "probe_failures"
    return fingerprint_from_series(series, 0.0, duration, segments=segments)


# --- alignment (pure) ----------------------------------------------------------------


def _total_overlap(spans: list[Span], targets: tuple[Span, ...], delta: float) -> float:
    """Σ |(span + delta) ∩ targets| via two-pointer sweep (both inputs sorted)."""
    total = 0.0
    j = 0
    n = len(targets)
    for s, e in spans:
        s += delta
        e += delta
        while j < n and targets[j][1] <= s:
            j += 1
        k = j
        while k < n and targets[k][0] < e:
            total += min(e, targets[k][1]) - max(s, targets[k][0])
            k += 1
    return total


def _prefilter(sub_spans: list[Span], windows: tuple[Span, ...]) -> list[Span]:
    """Cues that can intersect some window for some |δ| ≤ search bound."""
    zones = [(w0 - _SEARCH_S, w1 + _SEARCH_S) for w0, w1 in windows]
    out = []
    for s, e in sub_spans:
        if any(e > z0 and s < z1 for z0, z1 in zones):
            out.append((s, e))
    return out


def _score(
    sub: list[Span], fp: Fingerprint, delta: float, *, min_den: float = _MIN_DEN_S
) -> tuple[float, float, int]:
    """(score, sub_coverage_seconds, cues_in_window) at one δ.

    Score = BALANCED AGREEMENT of the two binary signals inside the probed windows:
    (speech∩cue + silence∩non-cue) / |windows|. Precision-only overlap flat-lined in
    Phase 0 (dialogue-dense windows → the base rate dominates and every δ scores the
    same); agreement also rewards CONCORDANT SILENCE, where the discriminating signal
    actually lives. For uncorrelated signals it sits near f·g+(1−f)(1−g); at the true
    δ it approaches 1."""
    w_total = sum(e - s for s, e in fp.windows)
    if w_total <= 0:
        return 0.0, 0.0, 0
    sub_in_w = _total_overlap(sub, fp.windows, delta)
    if sub_in_w < min_den:
        return 0.0, sub_in_w, 0
    speech_in_w = sum(e - s for s, e in fp.speech)  # speech is already window-clipped
    both = _total_overlap(sub, fp.speech, delta)
    agree = 2.0 * both + w_total - sub_in_w - speech_in_w
    cues = sum(1 for s, e in sub if any(e + delta > w0 and s + delta < w1 for w0, w1 in fp.windows))
    return agree / w_total, sub_in_w, cues


def _grid_best(
    sub: list[Span], fp: Fingerprint, center: float, half: float, step: float,
    *, min_den: float = _MIN_DEN_S,
):  # fmt: skip
    best = (-1.0, 0.0)  # (score, delta)
    curve: list[tuple[float, float]] = []
    d = center - half
    while d <= center + half + 1e-9:
        sc, _, _ = _score(sub, fp, d, min_den=min_den)
        curve.append((d, sc))
        if sc > best[0]:
            best = (sc, d)
        d += step
    return best[1], curve


def _local_best(sub: list[Span], fp_one: Fingerprint, center: float) -> float:
    d, _ = _grid_best(sub, fp_one, center, 3.0, 0.05, min_den=_MIN_DEN_ONE_S)
    return d


def _local_maxima(curve: list[tuple[float, float]], k: int = 6) -> list[float]:
    """Top-k separated local maxima deltas of a coarse curve (peaks ≥ 2 s apart)."""
    peaks = [
        (sc, d)
        for i, (d, sc) in enumerate(curve)
        if sc > 0
        and (i == 0 or curve[i - 1][1] <= sc)
        and (i == len(curve) - 1 or curve[i + 1][1] < sc)
    ]
    peaks.sort(reverse=True)
    picked: list[float] = []
    for _, d in peaks:
        if all(abs(d - q) >= 2.0 for q in picked):
            picked.append(d)
        if len(picked) >= k:
            break
    return picked


def align(sub_spans: list[Span] | tuple[Span, ...], fp: Fingerprint) -> Verdict:
    """Pure alignment of one candidate against the fingerprint. No I/O, no clock.

    CROSS-WINDOW VOTE (Phase-0 lesson): a global score curve over sparse windows is
    nearly flat and quasi-periodic dialogue creates spurious global peaks that beat the
    true one by a hair — but those peaks are INCONSISTENT across windows, while the true
    δ recurs in most of them. Each window therefore nominates its own top peaks; only
    δ clusters supported by a majority of windows are refined on the full fingerprint,
    and the runner-up is the best OTHER supported cluster (a real alternative, not
    curve noise)."""
    sub_all = sorted(sub_spans)
    if not sub_all:
        return Verdict(None, 1.0, "no_cues", None)
    sub = _prefilter(sub_all, fp.windows)
    if not sub:
        return Verdict(None, 1.0, "low_coverage", None)

    # 1. per-window peak nomination (each window sees only its own zone of cues)
    singles: list[Fingerprint] = [
        Fingerprint(fp.duration, (w,), _clip_speech(fp.speech, (w,))) for w in fp.windows
    ]
    nominations: list[list[float]] = []
    for fp1 in singles:
        local_sub = _prefilter(sub_all, fp1.windows)
        if not local_sub:
            nominations.append([])
            continue
        _, curve = _grid_best(local_sub, fp1, 0.0, _SEARCH_S, _STEPS[0], min_den=_MIN_DEN_ONE_S)
        nominations.append(_local_maxima(curve, _VOTE_PEAKS_K))

    # 2. cluster nominations across windows (≤ _VOTE_TOL_S apart = same candidate δ)
    flat = sorted(d for ps in nominations for d in ps)
    clusters: list[tuple[float, int]] = []  # (center, votes)
    for d in flat:
        for i, (c, n) in enumerate(clusters):
            if abs(d - c) <= _VOTE_TOL_S:
                clusters[i] = ((c * n + d) / (n + 1), n + 1)
                break
        else:
            clusters.append((d, 1))
    voting = sum(1 for ps in nominations if ps)
    need = max(3, (voting + 1) // 2)
    supported = sorted((c for c in clusters if c[1] >= need), key=lambda c: -c[1])
    if not supported:
        best_votes = max((n for _, n in clusters), default=0)
        diag = Alignment(0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, best_votes, voting)
        return Verdict(None, 1.0, "cross_window_disagree", diag)

    # 3. refine each supported cluster on the FULL fingerprint; best refined wins
    refined: list[tuple[float, float, int]] = []  # (score, delta, votes)
    for center, votes in supported:
        d1, _ = _grid_best(sub, fp, center, 1.5, _STEPS[1])
        d2, _ = _grid_best(sub, fp, d1, _STEPS[1] * 2, _STEPS[2])
        sc, _, _ = _score(sub, fp, d2)
        refined.append((sc, d2, votes))
    refined.sort(reverse=True)
    best_score, best_delta, best_votes = refined[0]
    _, coverage, cues = _score(sub, fp, best_delta)
    runner = refined[1][0] if len(refined) > 1 else 0.0

    # 4. split-half agreement (even vs odd informative windows) around the winner
    even = Fingerprint(fp.duration, fp.windows[0::2], _clip_speech(fp.speech, fp.windows[0::2]))
    odd = Fingerprint(fp.duration, fp.windows[1::2], _clip_speech(fp.speech, fp.windows[1::2]))
    split = 0.0
    if len(even.windows) >= 2 and len(odd.windows) >= 2:
        split = abs(_local_best(sub, even, best_delta) - _local_best(sub, odd, best_delta))

    # 5. drift: per-window local best vs window center, least-squares slope
    drift_total = 0.0
    pts: list[tuple[float, float]] = []
    for w, fp1 in zip(fp.windows, singles, strict=True):
        pts.append(((w[0] + w[1]) / 2, _local_best(sub, fp1, best_delta)))
    if len(pts) >= 3:
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        varx = sum((x - mx) ** 2 for x in xs)
        if varx > 0:
            slope = sum((x - mx) * (y - my) for x, y in pts) / varx
            drift_total = abs(slope) * fp.duration

    diag = Alignment(
        offset=best_delta, score=best_score, runner_up=runner, coverage_s=coverage,
        cues_in_window=cues, split_delta=split, drift_total_s=drift_total,
        votes=best_votes, windows_n=voting,
    )  # fmt: skip
    return _judge(diag)


def _clip_speech(speech: tuple[Span, ...], windows: tuple[Span, ...]) -> tuple[Span, ...]:
    out = []
    for s, e in speech:
        for w0, w1 in windows:
            lo, hi = max(s, w0), min(e, w1)
            if hi > lo:
                out.append((lo, hi))
    return tuple(out)


def _judge(a: Alignment) -> Verdict:
    """Multi-gate acceptance: every gate that fails is a typed, logged refusal. There
    is deliberately NO scalar confidence — accepted means every gate passed."""
    if a.coverage_s < _MIN_COVERAGE_S or a.cues_in_window < _MIN_CUES:
        return Verdict(None, 1.0, "low_coverage", a)
    if a.score < _MIN_SCORE:
        return Verdict(None, 1.0, "low_score", a)
    if a.votes < max(3, (a.windows_n + 1) // 2):
        return Verdict(None, 1.0, "cross_window_disagree", a)
    # runner_up is the best OTHER majority-supported cluster: a real alternative.
    if a.runner_up > 0 and (
        a.score - a.runner_up < _PEAK_DELTA or a.score / a.runner_up < _PEAK_RATIO
    ):
        return Verdict(None, 1.0, "ambiguous_peak", a)
    if a.split_delta > _MAX_SPLIT_S:
        return Verdict(None, 1.0, "split_half_disagree", a)
    if a.drift_total_s > _MAX_DRIFT_TOTAL_S:
        return Verdict(None, 1.0, "drift_suspected", a)
    if abs(a.offset) > _MAX_OFFSET_S:
        return Verdict(None, 1.0, "implausible_offset", a)
    return Verdict(a.offset, 1.0, "aligned", a)


# --- fixture (de)serialization: the recorded-dataset contract -------------------------


def fingerprint_to_dict(fp: Fingerprint) -> dict:
    return {
        "duration": fp.duration,
        "windows": [list(w) for w in fp.windows],
        "speech": [list(s) for s in fp.speech],
    }


def fingerprint_from_dict(d: dict) -> Fingerprint:
    return Fingerprint(
        duration=float(d["duration"]),
        windows=tuple((float(a), float(b)) for a, b in d["windows"]),
        speech=tuple((float(a), float(b)) for a, b in d["speech"]),
    )


if __name__ == "__main__":  # pragma: no cover — Phase-0 bench/recorder, see _bench.py
    import sys as _sys

    from . import _bench

    raise SystemExit(_bench.main(_sys.argv[1:]))
