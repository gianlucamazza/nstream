"""Phase-0 bench/recorder for the subtitle alignment engine (ADR 0020).

Dev tool, invoked as `python -m nstream.subalign <video_url> <srt>... [flags]`.
It exercises the REAL engine functions (no forked logic) against a live stream and:

- routes every ffmpeg read through a local counting proxy → exact transfer cost (G4)
  and the measured per-invocation container overhead (`_PROBE_OVERHEAD_B` calibration);
- `--g1`: anchor-accuracy gate — extracts overlapping windows and reports the speech-
  edge disagreement distribution (the `_to_abs` mapping must hold to ≤0.15 s);
- aligns every candidate SRT against one shared fingerprint, printing verdicts and
  (with `--truth`) PASS/FAIL against the human-validated ground truth (G3);
- `--tsv DIR`: dumps per-candidate coarse score curves for eyeballing peak shape;
- `--ablate`: probes generously once, then re-runs alignment on window subsets →
  confidence-margin vs probed-seconds curve (the minimum-viable-budget question);
- `--dump-fingerprint FILE`: records the fingerprint JSON that seeds the committed
  test fixture (`tests/data/coherence.json`).

Never prints the stream url (the proxy target stays in memory); output is stderr-free
plain stdout, safe to redirect into `docs/adr/0020-phase0/`.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import socketserver
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from . import srt, subalign


class _CountingProxy(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, target: str):
        self.target = target
        self.bytes_out = 0
        self.requests = 0
        self._lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _ProxyHandler)

    def tally(self, n: int) -> None:
        with self._lock:
            self.bytes_out += n
            self.requests += 1


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    server: _CountingProxy

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib signature
        pass  # quiet

    def do_GET(self) -> None:  # noqa: N802 — http.server contract
        req = urllib.request.Request(self.server.target)
        rng = self.headers.get("Range")
        if rng:
            req.add_header("Range", rng)
        req.add_header("User-Agent", "nstream-bench")
        try:
            with urllib.request.urlopen(req, timeout=30) as up:
                self.send_response(up.status)
                for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if up.headers.get(k):
                        self.send_header(k, up.headers[k])
                self.end_headers()
                n = 0
                while True:
                    chunk = up.read(256 * 1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break  # ffmpeg closed after -t: expected
                    n += len(chunk)
                self.server.tally(n)
        except OSError:
            with contextlib.suppress(OSError):
                self.send_error(502)


def _probe_media(url: str) -> tuple[float, int]:
    """(duration, size) — duration via the engine's own dependency chain (ffprobe
    through tracks would drag config; use oshash for size + ffprobe here directly)."""
    from . import oshash, tracks

    size = 0
    hashed = oshash.hash_url(url)
    if hashed:
        size = hashed[1]
    duration = tracks.probe_tracks(url).duration
    return duration, size


def _g1_anchor_check(url: str, windows: list[subalign.Span]) -> None:
    print("== G1: anchor accuracy (overlapping windows) ==")
    disagreements: list[float] = []
    for t0, t1 in windows[:5]:
        a = subalign._extract_window(url, (t0, t1))
        b = subalign._extract_window(url, (max(t0 - 7.0, 0.0), t1))
        if not a or not b:
            print(f"  window {t0:.0f}s: extraction failed on one variant (skip)")
            continue
        edges_a = [s for s, _ in a[1]] + [e for _, e in a[1]]
        edges_b = [s for s, _ in b[1]] + [e for _, e in b[1]]
        for ea in edges_a:
            best = min((abs(ea - eb) for eb in edges_b), default=None)
            if best is not None and best < 2.0:
                disagreements.append(best)
    if not disagreements:
        print("  NO matched edges — G1 cannot pass")
        return
    disagreements.sort()
    p90 = disagreements[int(len(disagreements) * 0.9) - 1]
    p50 = disagreements[len(disagreements) // 2]
    print(f"  matched edges: {len(disagreements)}  p50={p50:.3f}s  p90={p90:.3f}s")
    print(f"  G1 {'PASS' if p90 <= 0.15 else 'FAIL'} (p90 ≤ 0.15s required)")


def _overhead_kb(proxy: _CountingProxy, fp, size: int, duration: float) -> float:
    payload = sum(e - s for s, e in fp.windows) * size / duration
    return (proxy.bytes_out - payload) / max(proxy.requests, 1) / 1e3


def _fmt_verdict(v: subalign.Verdict) -> str:
    d = v.diag
    diag = (
        f" score={d.score:.3f} runner={d.runner_up:.3f} cov={d.coverage_s:.1f}s "
        f"cues={d.cues_in_window} split={d.split_delta:.2f}s drift={d.drift_total_s:.2f}s"
        if d
        else ""
    )
    off = f"{v.offset_s:+.2f}s" if v.offset_s is not None else "—"
    return f"{v.reason:22s} δ={off}{diag}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m nstream.subalign", description=__doc__)
    ap.add_argument("url")
    ap.add_argument("srts", nargs="+")
    ap.add_argument("--truth", help='ground truth "i=offset,..." per candidate index')
    ap.add_argument("--dump-fingerprint", metavar="FILE")
    ap.add_argument(
        "--fingerprint", metavar="FILE", help="reuse a recorded fingerprint (no probing)"
    )
    ap.add_argument("--tsv", metavar="DIR", help="dump coarse score curves per candidate")
    ap.add_argument("--g1", action="store_true", help="anchor-accuracy gate")
    ap.add_argument("--ablate", action="store_true", help="window-subset ablation")
    ap.add_argument("--budget-mb", type=int, default=25)
    ap.add_argument("--no-proxy", action="store_true")
    args = ap.parse_args(argv)

    url = args.url
    proxy: _CountingProxy | None = None
    if not args.no_proxy:
        proxy = _CountingProxy(url)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        parts = urlsplit(url)
        url = f"http://127.0.0.1:{proxy.server_address[1]}{parts.path or '/'}"
        if parts.query:
            url += f"?{parts.query}"
        # the proxy re-targets the ORIGINAL url regardless of path: adjust handler target
        proxy.target = args.url

    duration, size = _probe_media(args.url)
    print(f"media: duration={duration:.1f}s size={size} bytes ({size / duration / 1024:.0f} KB/s)")

    cue_sets = [srt.cue_spans(p) for p in args.srts]
    for i, (p, spans) in enumerate(zip(args.srts, cue_sets, strict=True)):
        print(f"candidate[{i}]: {p}  cues={len(spans)}")

    if args.fingerprint:
        with open(args.fingerprint) as f:
            fp = subalign.fingerprint_from_dict(json.load(f))
        print(f"fingerprint: RIUSATO da {args.fingerprint}")
    else:
        t_start = time.monotonic()
        got = subalign.probe(
            url, duration, size,
            budget_s=120.0, budget_bytes=args.budget_mb * 1_000_000,
            cue_starts=[s for s, _ in cue_sets[0]],
        )  # fmt: skip
        wall = time.monotonic() - t_start
        if isinstance(got, str):
            print(f"PROBE REFUSED: {got}")
            return 1
        fp = got
        print(
            f"fingerprint: windows={len(fp.windows)} speech_spans={len(fp.speech)} "
            f"speech_s={sum(e - s for s, e in fp.speech):.1f} wall={wall:.1f}s"
        )
        if proxy:
            print(
                f"G4 transfer: {proxy.bytes_out / 1e6:.1f} MB in {proxy.requests} requests "
                f"(budget {args.budget_mb} MB) → overhead/invocation ≈ "
                f"{_overhead_kb(proxy, fp, size, duration):.0f} KB"
            )

    if args.dump_fingerprint:
        with open(args.dump_fingerprint, "w") as f:
            json.dump(subalign.fingerprint_to_dict(fp), f, indent=1)
        print(f"fingerprint scritto → {args.dump_fingerprint}")

    if args.g1:
        plan = subalign.plan_probes(duration, size, [s for s, _ in cue_sets[0]])
        if isinstance(plan, list):
            _g1_anchor_check(url, plan)

    truth: dict[int, float] = {}
    if args.truth:
        for part in args.truth.split(","):
            k, v = part.split("=")
            truth[int(k)] = float(v)

    print("== verdicts ==")
    ok = True
    for i, spans in enumerate(cue_sets):
        v = subalign.align(spans, fp)
        line = f"[{i}] {_fmt_verdict(v)}"
        if i in truth:
            expected = truth[i]
            if v.reason == "aligned" and v.offset_s is not None:
                good = abs(v.offset_s - expected) <= 0.7
                line += f"  truth={expected:+.1f}s → {'PASS' if good else 'FAIL(WRONG δ)'}"
                ok &= good
            else:
                line += f"  truth={expected:+.1f}s → REFUSED ({v.reason})"
        print(line)
        if args.tsv:
            import os

            os.makedirs(args.tsv, exist_ok=True)
            sub = subalign._prefilter(sorted(spans), fp.windows)
            with open(os.path.join(args.tsv, f"curve-{i}.tsv"), "w") as f:
                f.write("delta\tscore\n")
                d = -subalign._SEARCH_S
                while d <= subalign._SEARCH_S:
                    sc, _, _ = subalign._score(sub, fp, d)
                    f.write(f"{d:.2f}\t{sc:.4f}\n")
                    d += 0.25

    if args.ablate:
        print("== ablation (window subsets, candidate 0) ==")
        spans = cue_sets[0]
        for k in range(4, len(fp.windows) + 1, 2):
            sub_fp = subalign.Fingerprint(
                fp.duration, fp.windows[:k], subalign._clip_speech(fp.speech, fp.windows[:k])
            )
            v = subalign.align(spans, sub_fp)
            secs = sum(e - s for s, e in sub_fp.windows)
            print(f"  {k} windows ({secs:.0f}s audio): {_fmt_verdict(v)}")

    return 0 if ok else 2
