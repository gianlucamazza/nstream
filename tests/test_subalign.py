"""Unit tests for the native sparse-evidence alignment engine (ADR 0020).

The `align` half is pure math: these tests build synthetic fingerprints with KNOWN
offsets and assert exact recovery, honest refusals, and the sign convention that burned
us in the field (late subs → negative offset to ADD)."""

from __future__ import annotations

import random

import pytest

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


# --- probe planning -------------------------------------------------------------


def test_plan_probes_low_bitrate_full_fleet():
    # 1.5 GB / 2 h ≈ 208 KB/s → 10 windows, ~9 s each, inside budget
    plan = subalign.plan_probes(7200.0, 1_500_000_000, [float(t) for t in range(100, 7000, 12)])
    assert isinstance(plan, list) and len(plan) == 10
    d = plan[0][1] - plan[0][0]
    assert 4.0 <= d <= 30.0
    est = sum(e - s for s, e in plan) * (1_500_000_000 / 7200.0) + len(plan) * 600_000
    assert est <= 25_000_000 * 1.05


def test_plan_probes_high_bitrate_degrades_probe_count():
    # the Coherence regime: ~776 KB/s → fewer, shorter windows, still ≥ _N_MIN
    plan = subalign.plan_probes(5276.4, 4_093_671_584, [float(t) for t in range(30, 5200, 3)])
    assert isinstance(plan, list)
    assert subalign._N_MIN <= len(plan) <= 10


def test_plan_probes_refusals():
    assert subalign.plan_probes(0.0, 100, []) == "no_media_geometry"
    assert subalign.plan_probes(3600.0, 0, []) == "no_media_geometry"
    # absurd byte-rate: 40 GB for 10 minutes → over budget
    assert subalign.plan_probes(600.0, 40_000_000_000, []) == "bitrate_over_budget"


def test_plan_probes_windows_inside_usable_range():
    dur = 6000.0
    plan = subalign.plan_probes(dur, 2_000_000_000, [float(t) for t in range(0, 6000, 10)])
    assert isinstance(plan, list)
    for t0, t1 in plan:
        assert dur * 0.03 - 10.1 <= t0 and t1 <= dur * 0.97 + 0.1


# --- signal parsing (stubbed ffmpeg) ----------------------------------------------


_RMS_STDERR = "\n".join(
    f"[Parsed_ametadata_2 @ 0x1] frame:{i} pts:{i * 4410} pts_time:{i * 0.1:.2f}\n"
    f"[Parsed_ametadata_2 @ 0x1] lavfi.astats.Overall.RMS_level="
    + ("-30.0" if 10 <= i < 30 or 50 <= i < 70 else "-60.0")
    for i in range(90)
)


class _Proc:
    def __init__(self, err: str, rc: int = 0):
        self.returncode, self.stdout, self.stderr = rc, "", err


def test_extract_window_rms_primary(monkeypatch):
    monkeypatch.setattr(subalign.util, "run_cmd", lambda *a, **k: _Proc(_RMS_STDERR))
    got = subalign._extract_window("http://u", (100.0, 109.0))
    assert got is not None
    eff, spans = got
    assert eff[0] == pytest.approx(100.0 + 0.3 + 0.4)
    # two speech bumps at rel 1.0-3.0 and 5.0-7.0 → absolute ~101/105
    assert len(spans) == 2
    assert spans[0][0] == pytest.approx(101.0, abs=0.3)
    assert spans[1][0] == pytest.approx(105.0, abs=0.3)


def test_extract_window_silencedetect_fallback(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if "ametadata" in " ".join(cmd):
            return _Proc("no rms lines here")
        return _Proc(
            "[silencedetect @ 0x1] silence_start: 0.0\n"
            "[silencedetect @ 0x1] silence_end: 2.5 | silence_duration: 2.5\n"
            "[silencedetect @ 0x1] silence_start: 5.5\n"
            "[silencedetect @ 0x1] silence_end: 8.8 | silence_duration: 3.3\n"
        )

    monkeypatch.setattr(subalign.util, "run_cmd", fake)
    got = subalign._extract_window("http://u", (200.0, 209.0))
    assert got is not None
    _, spans = got
    assert spans and spans[0][0] == pytest.approx(202.5, abs=0.2)


def test_extract_window_uninformative_is_none(monkeypatch):
    loud = "\n".join(
        f"pts_time:{i * 0.1:.2f}\nlavfi.astats.Overall.RMS_level=-20.0" for i in range(90)
    )
    monkeypatch.setattr(subalign.util, "run_cmd", lambda *a, **k: _Proc(loud))
    assert subalign._extract_window("http://u", (0.0, 9.0)) is None  # wall-to-wall ≥0.90


def test_probe_needs_enough_windows(monkeypatch):
    monkeypatch.setattr(subalign, "plan_probes", lambda *a, **k: [(0.0, 9.0)] * 6)
    monkeypatch.setattr(subalign, "_extract_window", lambda url, w: None)
    monkeypatch.setattr(subalign, "available", lambda: True)
    got = subalign.probe("http://u", 3600.0, 10**9, cue_starts=[])
    assert got == "probe_failures"


def test_probe_assembles_fingerprint(monkeypatch):
    windows = [(float(i * 100), float(i * 100 + 9)) for i in range(6)]
    monkeypatch.setattr(subalign, "plan_probes", lambda *a, **k: list(windows))
    monkeypatch.setattr(
        subalign, "_extract_window",
        lambda url, w: ((w[0] + 0.7, w[1] - 0.4), [(w[0] + 1, w[0] + 3)]),
    )  # fmt: skip
    monkeypatch.setattr(subalign, "available", lambda: True)
    fp = subalign.probe("http://u", 3600.0, 10**9, cue_starts=[])
    assert isinstance(fp, Fingerprint)
    assert len(fp.windows) == 6 and len(fp.speech) == 6


def test_fingerprint_dict_roundtrip():
    fp = _mk_truth(n_windows=4)
    again = subalign.fingerprint_from_dict(subalign.fingerprint_to_dict(fp))
    assert again == fp
