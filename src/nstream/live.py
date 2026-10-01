"""Live HLS-TS producer for the Tier-2 cast (ADR 0039).

The DMR plays a growing EVENT playlist of MPEG-TS segments (video copied, audio AAC) as soon
as the first segments exist, so a cast that needs an audio conversion starts in seconds
instead of after the whole-file remux (ADR 0005). This module owns the ffmpeg producer and
the disk policy around it; `serve` serves the directory and reports which segment the
receiver asked for, `remux.cast_live` wires it to a cast.

Disk stays bounded without polling the receiver:
- **behind**: segments older than `KEEP_BEHIND_S` before the newest one requested are
  deleted (a backward seek past that window is not served);
- **ahead**: the producer is paused (SIGSTOP) when it runs `AHEAD_MAX_S` past the newest
  request, and resumed (SIGCONT) below `AHEAD_RESUME_S`. A paused producer's upstream may
  time out; `urlproxy` resumes it with a Range.

The job (the debrid url) reaches the producer as data, never as an argv: ffmpeg reads a
`urlproxy.local_url` of the process that runs it.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import log, urlproxy, util

_log = log.get_logger("live")

PLAYLIST = "index.m3u8"
SEGMENT_S = 6
KEEP_BEHIND_S = 600.0
AHEAD_MAX_S = 1800.0
AHEAD_RESUME_S = 900.0
_SEGMENT = re.compile(r"index(\d+)\.ts$")
_EXTINF = re.compile(r"#EXTINF:([0-9.]+)")


@dataclass(frozen=True)
class Job:
    """What to produce: the source url, the audio mapping/encoding (ffmpeg args) and the
    initial play head `head_s` (the resume point the LOAD will seek to: the pacing must let
    the producer reach it, and nothing before it needs to stay on disk)."""

    url: str
    audio_map: str = "0:a:0?"
    audio_args: tuple[str, ...] = ("-c:a", "aac", "-ac", "2", "-b:a", "192k")
    head_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "url": self.url, "audio_map": self.audio_map,
            "audio_args": list(self.audio_args), "head_s": self.head_s,
        }  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict) -> Job:
        return cls(
            str(d["url"]), str(d["audio_map"]),
            tuple(str(a) for a in d["audio_args"]), float(d.get("head_s") or 0.0),
        )  # fmt: skip


def producer_cmd(src: str, out_dir: str, job: Job) -> list[str]:
    """The ffmpeg argv: video copied, one audio track, MPEG-TS segments in a growing EVENT
    playlist. `temp_file` renames each segment and playlist into place, so the server never
    serves a half-written file. `src` must already be token-free (a loopback url)."""
    return [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-rw_timeout", "30000000", "-i", src,
        "-map", "0:v:0", "-map", job.audio_map, "-c:v", "copy", *job.audio_args,
        "-f", "hls", "-hls_time", str(SEGMENT_S), "-hls_list_size", "0",
        "-hls_playlist_type", "event", "-hls_segment_type", "mpegts",
        "-hls_flags", "temp_file",
        "-hls_segment_filename", os.path.join(out_dir, "index%d.ts"),
        os.path.join(out_dir, PLAYLIST),
    ]  # fmt: skip


def timeline(out_dir: str) -> list[tuple[int, float, float]]:
    """(segment index, start s, duration s) of every segment the playlist lists so far."""
    try:
        with open(os.path.join(out_dir, PLAYLIST), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out: list[tuple[int, float, float]] = []
    t = 0.0
    dur = 0.0
    for line in lines:
        m = _EXTINF.match(line)
        if m:
            dur = float(m.group(1))
            continue
        s = _SEGMENT.search(line)
        if s:
            out.append((int(s.group(1)), t, dur))
            t += dur
    return out


def produced_s(out_dir: str) -> float:
    """Seconds of media the playlist already lists."""
    tl = timeline(out_dir)
    return tl[-1][1] + tl[-1][2] if tl else 0.0


def segment_index(name: str) -> int | None:
    m = _SEGMENT.fullmatch(name)
    return int(m.group(1)) if m else None


@dataclass
class Producer:
    """A running producer and its disk policy. `on_request` is called by the server for
    each segment served; `tick` runs the pacing periodically; `stop` ends it all."""

    out_dir: str
    proc: subprocess.Popen
    newest_s: float = 0.0
    paused: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def start(cls, job: Job, out_dir: str) -> Producer | None:
        cmd = producer_cmd(urlproxy.local_url(job.url), out_dir, job)
        try:
            # stderr beside the segments (never served: the route whitelists names), read
            # back by `failure_reason`, removed with the directory.
            with open(os.path.join(out_dir, "ffmpeg.log"), "wb") as err:
                proc = subprocess.Popen(  # noqa: S603
                    cmd, stdout=subprocess.DEVNULL, stderr=err,
                    preexec_fn=util.die_with_parent,  # noqa: PLW1509 — owned by this process
                )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as e:
            _log.warning("live: ffmpeg non avviato (%s)", type(e).__name__)
            return None
        return cls(out_dir, proc, newest_s=job.head_s)

    def failed(self) -> bool:
        """The producer exited without producing anything usable."""
        return self.proc.poll() not in (None, 0) and not timeline(self.out_dir)

    def failure_reason(self) -> str:
        """The tail of ffmpeg's stderr (diagnosis only; it never carries the token, ffmpeg
        only knows the loopback url)."""
        try:
            with open(os.path.join(self.out_dir, "ffmpeg.log"), "rb") as f:
                return f.read()[-300:].decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def on_request(self, index: int) -> None:
        """Segment `index` was requested: advance the play head, prune behind it."""
        tl = timeline(self.out_dir)
        start = next((t for i, t, _ in tl if i == index), None)
        if start is None:
            return
        with self._lock:
            self.newest_s = max(self.newest_s, start)
            floor = self.newest_s - KEEP_BEHIND_S
        for i, t, d in tl:
            if t + d > floor:
                break
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(self.out_dir, f"index{i}.ts"))

    def tick(self) -> None:
        """Pause the producer far ahead of the play head, resume it when it catches up."""
        if self.proc.poll() is not None:
            return
        ahead = produced_s(self.out_dir) - self.newest_s
        with self._lock:
            if not self.paused and ahead > AHEAD_MAX_S:
                self._signal(signal.SIGSTOP)
                self.paused = True
            elif self.paused and ahead < AHEAD_RESUME_S:
                self._signal(signal.SIGCONT)
                self.paused = False

    def _signal(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            self.proc.send_signal(sig)

    def run_pacing(self, interval: float = 2.0) -> threading.Thread:
        """Start the pacing loop in a daemon thread (ends with the producer)."""

        def loop() -> None:
            while self.proc.poll() is None:
                self.tick()
                time.sleep(interval)

        t = threading.Thread(target=loop, name="nstream-live-pacing", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        """Terminate ffmpeg (resuming it first, a stopped process ignores SIGTERM until
        continued) and remove the segment directory."""
        if self.proc.poll() is None:
            self._signal(signal.SIGCONT)
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.out_dir, ignore_errors=True)
