"""Measured link throughput store for the live Tier-2 (ADR 0039)."""

from __future__ import annotations

from nstream.state import throughput


def test_latest_is_the_most_recent_fresh_measurement():
    assert throughput.latest() == 0.0  # unknown
    throughput.record("real-debrid.com", 2.4e6)
    throughput.record("other.net", 9e6)
    assert throughput.latest() == 9e6
    assert throughput.latest(now=__import__("time").time() + 30 * 86400) == 0.0  # stale


def test_record_ignores_nonsense():
    throughput.record("", 1e6)
    throughput.record("x.com", 0)
    assert throughput.latest() == 0.0
