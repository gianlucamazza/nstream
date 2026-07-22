"""End-to-end smoke tests: run `nstream` as a real subprocess and assert the `--json`
harness contract holds against an actual invocation, not a mocked in-process call.

The rest of the suite is unit-level with mocks (the cast decision tree, selection, etc.).
This file is the one place that spawns the CLI for real, guarding the invariants the
`nstream` skill relies on: **exactly one JSON object on stdout, nothing else** (no banner,
no traceback, no leaked stream/debrid URL or token), and a non-zero exit on a usage error.

Only deterministic, network-free paths are exercised here (argument-validation errors),
so the tests stay hermetic — no addon HTTP, no external tool, no Chromecast."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

# Argument-validation paths: each prints exactly one JSON object and exits non-zero, with no
# network I/O — the hermetic slice of the `--json` surface.
_USAGE_CASES = [
    (["--json", "--mirror", "--no-mirror", "x"], "incompatibili"),
    (["--json", "--quality", "bogus", "x"], "non valida"),
]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nstream", *args],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip


@pytest.mark.parametrize("args,needle", _USAGE_CASES)
def test_json_usage_error_is_single_object(args, needle):
    proc = _run(args)
    # Exactly one JSON object on stdout — json.loads over the whole stripped stdout must parse
    # (a trailing banner/second object would make this raise), and it is a usage error.
    out = proc.stdout.strip()
    obj = json.loads(out)
    assert isinstance(obj, dict)
    assert obj["ok"] is False and obj["error"] == "usage"
    assert needle in obj["message"]
    assert proc.returncode != 0


@pytest.mark.parametrize("args,_needle", _USAGE_CASES)
def test_json_stdout_never_leaks_token_or_traceback(args, _needle):
    proc = _run(args)
    # The JSON-purity invariant: no traceback, no Torrentio/debrid URL or token on stdout.
    assert "Traceback" not in proc.stdout
    for marker in ("torrentio", "realdebrid", "/resolve/", "http://", "https://"):
        assert marker not in proc.stdout.lower()
