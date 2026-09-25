"""Reproducible offline microbenchmarks; compare only on the same host/interpreter."""

import argparse
import hashlib
import json
import platform
import statistics
import time
from pathlib import Path

from nstream import api, quality
from nstream.types import Stream


def measure():
    streams: list[Stream] = [
        {
            "name": f"Fixture {i}",
            "title": f"Fixture.{i}.1080p.H264.ITA.WEB-DL",
            "url": f"http://127.0.0.1/{i}.mp4",
        }
        for i in range(2000)
    ]
    caps, spec = quality.HwCaps(), quality.FilterSpec()
    timings: dict[str, list[float]] = {"ranking": [], "gather": []}
    fingerprint = ""
    for _ in range(5):
        quality._PARSE_CACHE.clear()
        started = time.perf_counter()
        ranked, excluded = quality.rank_streams(streams, caps, spec)
        timings["ranking"].append((time.perf_counter() - started) * 1000)
        fingerprint = hashlib.sha256(
            json.dumps([[row.stream["url"] for row in ranked], len(excluded)]).encode()
        ).hexdigest()
        started = time.perf_counter()
        assert api._gather([lambda: [1] for _ in range(8)]) == [1] * 8
        timings["gather"].append((time.perf_counter() - started) * 1000)
    return {
        "schema": 1,
        "host": platform.node(),
        "python": platform.python_version(),
        "ranking_fingerprint": fingerprint,
        "median_ms": {key: statistics.median(values) for key, values in timings.items()},
        "samples_ms": timings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    result = measure()
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if args.compare:
        baseline = json.loads(args.compare.read_text())
        for key in ("host", "python", "ranking_fingerprint"):
            if baseline[key] != result[key]:
                raise SystemExit(f"incompatible baseline: {key}")
        for key, elapsed in result["median_ms"].items():
            if elapsed > baseline["median_ms"][key] * 1.1:
                raise SystemExit(f"regression greater than 10%: {key}")


if __name__ == "__main__":
    main()
