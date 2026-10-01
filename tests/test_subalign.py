"""Unit tests for the native sparse-evidence alignment engine (ADR 0020).

The `align` half is pure math: these tests build synthetic fingerprints with KNOWN
offsets and assert exact recovery, honest refusals, and the sign convention that burned
us in the field (late subs → negative offset to ADD)."""

from __future__ import annotations

import random

from nstream import subalign
from nstream.subalign import Fingerprint, Span


def _mk_truth(
    n_windows: int = 8, win_len: float = 30.0, spacing: float = 600.0, seed: int = 42
) -> Fingerprint:
    """A synthetic media fingerprint: n windows across a long runtime, each holding a
    handful of IRREGULARLY spaced speech spans (regular grids create periodic score
    peaks → false ambiguity)."""
    rng = random.Random(seed)
    windows: list[Span] = []
    speech: list[Span] = []
    for i in range(n_windows):
        t0 = 100.0 + i * spacing
        windows.append((t0, t0 + win_len))
        t = t0 + rng.uniform(0.5, 2.0)
        while t < t0 + win_len - 3.0:
            d = rng.uniform(1.2, 3.0)
            speech.append((t, t + d))
            t += d + rng.uniform(0.8, 4.0)
    duration = windows[-1][1] + 400.0
    return Fingerprint(duration=duration, windows=tuple(windows), speech=tuple(speech))


def _subs_from_speech(fp: Fingerprint, *, late_by: float = 0.0, jitter: float = 0.15,
                      seed: int = 7) -> list[Span]:  # fmt: skip
    """Subtitle cues mirroring the speech, LATE by `late_by` seconds (cue times larger).
    The correct verdict is offset = -late_by (added to sub times → speech times)."""
    rng = random.Random(seed)
    out = []
    for s, e in fp.speech:
        j = rng.uniform(-jitter, jitter)
        out.append((s + late_by + j, e + late_by + j))
    return out


def test_align_recovers_zero_offset():
    fp = _mk_truth()
    v = subalign.align(_subs_from_speech(fp), fp)
    assert v.reason == "aligned" and v.offset_s is not None
    assert abs(v.offset_s) <= 0.3


def test_align_recovers_late_family_with_negative_offset():
    """THE field case: subs 14 s late must yield offset ≈ -14 (sign convention)."""
    fp = _mk_truth()
    v = subalign.align(_subs_from_speech(fp, late_by=14.0), fp)
    assert v.reason == "aligned" and v.offset_s is not None
    assert abs(v.offset_s - (-14.0)) <= 0.3


def test_align_two_families_both_converge_to_same_timing():
    """Two timing families of the same translation: both align, offsets differ by the
    family gap — after retime both land on the same absolute timing."""
    fp = _mk_truth()
    va = subalign.align(_subs_from_speech(fp, late_by=0.0), fp)
    vb = subalign.align(_subs_from_speech(fp, late_by=14.0), fp)
    assert va.reason == vb.reason == "aligned"
    assert va.offset_s is not None and vb.offset_s is not None
    assert abs((vb.offset_s - va.offset_s) - (-14.0)) <= 0.4


def test_align_refuses_random_noise():
    """Cues uncorrelated with the speech must NOT produce a confident verdict."""
    fp = _mk_truth()
    rng = random.Random(99)
    noise = []
    t = 50.0
    while t < fp.duration - 10:
        d = rng.uniform(1.0, 3.0)
        noise.append((t, t + d))
        t += d + rng.uniform(1.0, 6.0)
    v = subalign.align(noise, fp)
    assert v.reason != "aligned" and v.offset_s is None
    assert v.diag is not None  # refusal still carries the diagnostics


def test_align_refuses_no_cues_and_out_of_window_cues():
    fp = _mk_truth()
    assert subalign.align([], fp).reason == "no_cues"
    far = [(fp.duration + 1000 + i * 10.0, fp.duration + 1002 + i * 10.0) for i in range(40)]
    assert subalign.align(far, fp).reason == "low_coverage"


def test_align_refuses_split_half_disagreement():
    """Even and odd windows suggesting different offsets = internal contradiction —
    the exact 'windows disagree' noise the field produced. Must refuse."""
    fp = _mk_truth(n_windows=8)
    subs = []
    for s, e in fp.speech:
        win_idx = next(i for i, (w0, w1) in enumerate(fp.windows) if s >= w0 - 1 and e <= w1 + 1)
        shift = 0.0 if win_idx % 2 == 0 else 3.0  # odd windows: subs 3 s late
        subs.append((s + shift, e + shift))
    v = subalign.align(subs, fp)
    assert v.reason in ("split_half_disagree", "ambiguous_peak", "low_score")
    assert v.offset_s is None


def test_align_refuses_progressive_drift():
    """A linear fps-style drift must refuse (drift_suspected), never deliver a wrong
    constant. Drift correction is v2, behind its own gate."""
    fp = _mk_truth(n_windows=10)
    subs = [(s + s * 0.004, e + e * 0.004) for s, e in fp.speech]  # ~0.4% drift
    v = subalign.align(subs, fp)
    assert v.reason in ("drift_suspected", "split_half_disagree", "ambiguous_peak", "low_score")
    assert v.offset_s is None


def test_judge_gate_reasons_are_typed():
    assert {
        "aligned", "low_score", "ambiguous_peak", "split_half_disagree",
        "drift_suspected", "implausible_offset", "low_coverage", "no_cues",
    } <= subalign._REASONS  # fmt: skip


def test_fingerprint_dict_roundtrip():
    fp = _mk_truth(n_windows=4)
    again = subalign.fingerprint_from_dict(subalign.fingerprint_to_dict(fp))
    assert again == fp
