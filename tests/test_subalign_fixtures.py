"""Recorded-dataset acceptance tests for the alignment engine (ADR 0020, Phase 0).

These fixtures ARE the permanent gate: `align()` is pure math, so replaying the
field-recorded fingerprints against the real candidate cue spans pins the engine's
verdicts forever — the must-accept case (Moon: normal dialogue film, exact offset
recovery incl. synthetic shifts) and the must-refuse cases (Coherence: pathological
wall-to-wall chatter — zero confident-wrong, the cardinal contract, on BOTH the sparse
remote fingerprint and the full local one). Re-record via `python -m nstream.subalign`
(remote) or `subalign.probe_local` (local); provenance in each fixture's header."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nstream import subalign

_DATA = Path(__file__).parent / "data"


def _load(name: str):
    with open(_DATA / name) as f:
        d = json.load(f)
    return subalign.fingerprint_from_dict(d["fingerprint"]), d["candidates"]


def _spans(c) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in c["cue_spans"]]


def _check(fp, cands):
    wrong = []
    for name, c in cands.items():
        v = subalign.align(_spans(c), fp)
        exp = c["expect"]
        if exp["reason"] == "aligned":
            assert v.reason == "aligned", f"{name}: atteso aligned, avuto {v.reason}"
            assert v.offset_s is not None
            assert abs(v.offset_s - exp["offset"]) <= exp["tol"], f"{name}: δ={v.offset_s}"
        else:
            if v.reason == "aligned":
                wrong.append((name, v.offset_s))
    assert not wrong, f"CONFIDENT-WRONG (violazione del contratto cardinale): {wrong}"


def test_moon_local_accepts_and_recovers_offsets():
    fp, cands = _load("moon_local.json")
    _check(fp, cands)


def test_moon_local_recovers_synthetic_shifts():
    """Self-validating ground truth: shifting the accepted sub by K must move the
    verdict by exactly -K (the field sign convention: late subs → negative offset)."""
    fp, cands = _load("moon_local.json")
    with open(_DATA / "moon_local.json") as f:
        shifts = json.load(f)["synthetic_shifts"]
    base = _spans(cands["sub0_ita"])
    v0 = subalign.align(base, fp)
    assert v0.reason == "aligned"
    for k in shifts:
        shifted = [(s + k, e + k) for s, e in base]
        v = subalign.align(shifted, fp)
        assert v.reason == "aligned", f"shift {k}: {v.reason}"
        assert v.offset_s == pytest.approx(v0.offset_s - k, abs=0.35), f"shift {k}"


def test_coherence_sparse_refuses_everything():
    """Sparse remote evidence (Phase-0 G3): measured insufficient — every candidate
    refuses. This pins the honest behavior of the bench-only sparse mode."""
    fp, cands = _load("coherence_sparse.json")
    _check(fp, cands)


def test_coherence_local_refuses_everything():
    """Even FULL local signal must refuse on the pathological film (overlapping
    untranslated chatter): correct is impossible to certify, so nothing is certified."""
    fp, cands = _load("coherence_local.json")
    _check(fp, cands)
