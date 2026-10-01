"""Sparse remote probing (bench-only, ADR 0020): planning, window extraction, assembly."""

from __future__ import annotations

import pytest

from nstream import _subalign_remote, subalign
from nstream.subalign import Fingerprint

# --- probe planning -------------------------------------------------------------


def test_plan_probes_low_bitrate_full_fleet():
    # 1.5 GB / 2 h ≈ 208 KB/s → 10 windows, ~9 s each, inside budget
    plan = _subalign_remote.plan_probes(
        7200.0, 1_500_000_000, [float(t) for t in range(100, 7000, 12)]
    )
    assert isinstance(plan, list) and len(plan) == 10
    d = plan[0][1] - plan[0][0]
    assert 4.0 <= d <= 30.0
    est = sum(e - s for s, e in plan) * (1_500_000_000 / 7200.0) + len(plan) * 600_000
    assert est <= 25_000_000 * 1.05


def test_plan_probes_high_bitrate_degrades_probe_count():
    # the Coherence regime: ~776 KB/s → fewer, shorter windows, still ≥ _N_MIN
    plan = _subalign_remote.plan_probes(
        5276.4, 4_093_671_584, [float(t) for t in range(30, 5200, 3)]
    )
    assert isinstance(plan, list)
    assert _subalign_remote._N_MIN <= len(plan) <= 10


def test_plan_probes_refusals():
    assert _subalign_remote.plan_probes(0.0, 100, []) == "no_media_geometry"
    assert _subalign_remote.plan_probes(3600.0, 0, []) == "no_media_geometry"
    # absurd byte-rate: 40 GB for 10 minutes → over budget
    assert _subalign_remote.plan_probes(600.0, 40_000_000_000, []) == "bitrate_over_budget"


def test_plan_probes_windows_inside_usable_range():
    dur = 6000.0
    plan = _subalign_remote.plan_probes(dur, 2_000_000_000, [float(t) for t in range(0, 6000, 10)])
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
    monkeypatch.setattr(_subalign_remote.util, "run_cmd", lambda *a, **k: _Proc(_RMS_STDERR))
    got = _subalign_remote._extract_window("http://u", (100.0, 109.0))
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

    monkeypatch.setattr(_subalign_remote.util, "run_cmd", fake)
    got = _subalign_remote._extract_window("http://u", (200.0, 209.0))
    assert got is not None
    _, spans = got
    assert spans and spans[0][0] == pytest.approx(202.5, abs=0.2)


def test_extract_window_uninformative_is_none(monkeypatch):
    loud = "\n".join(
        f"pts_time:{i * 0.1:.2f}\nlavfi.astats.Overall.RMS_level=-20.0" for i in range(90)
    )
    monkeypatch.setattr(_subalign_remote.util, "run_cmd", lambda *a, **k: _Proc(loud))
    assert _subalign_remote._extract_window("http://u", (0.0, 9.0)) is None  # wall-to-wall ≥0.90


def test_probe_needs_enough_windows(monkeypatch):
    monkeypatch.setattr(_subalign_remote, "plan_probes", lambda *a, **k: [(0.0, 9.0)] * 6)
    monkeypatch.setattr(_subalign_remote, "_extract_window", lambda url, w: None)
    monkeypatch.setattr(subalign, "available", lambda: True)
    got = _subalign_remote.probe("http://u", 3600.0, 10**9, cue_starts=[])
    assert got == "probe_failures"


def test_probe_assembles_fingerprint(monkeypatch):
    windows = [(float(i * 100), float(i * 100 + 9)) for i in range(6)]
    monkeypatch.setattr(_subalign_remote, "plan_probes", lambda *a, **k: list(windows))
    monkeypatch.setattr(
        _subalign_remote, "_extract_window",
        lambda url, w: ((w[0] + 0.7, w[1] - 0.4), [(w[0] + 1, w[0] + 3)]),
    )  # fmt: skip
    monkeypatch.setattr(subalign, "available", lambda: True)
    fp = _subalign_remote.probe("http://u", 3600.0, 10**9, cue_starts=[])
    assert isinstance(fp, Fingerprint)
    assert len(fp.windows) == 6 and len(fp.speech) == 6
