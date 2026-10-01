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
import dataclasses
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

PLAYLIST = "index.m3u8"  # generation 0 (the playlist a cast LOADs first)
SEGMENT_S = 6
KEEP_BEHIND_S = 600.0
AHEAD_MAX_S = 1800.0
AHEAD_RESUME_S = 900.0
# Generation 0 keeps the historical names; a restart (seek anywhere) writes generation N
# as `gN.m3u8` + `gN_<i>.ts`, so the receiver never mixes two producers' segments.
_SEGMENT = re.compile(r"(?:index|g(\d+)_)(\d+)\.ts$")
_PLAYLIST_NAME = re.compile(r"(?:index|g\d+)\.m3u8")


# A playlist from the film's start begins at ffmpeg's mux delay (~1.4 s), not 0: below
# this, no placeholder (fast resumes start past 60 s anyway).
_GAP_MIN_S = 5.0


def film_time_playlist(text: str, base: float) -> str:
    """The playlist as served to the receiver: when it starts mid-film (`base` > 1 s, a
    fast resume or a restart), an `EXT-X-GAP` placeholder covering 0..base comes first, so
    the receiver's timeline — its clock, its progress bar, the positions it reports — is
    film time. Without it the TV showed 0:00 after every resume/seek (field 2026-10-02);
    verified on the 43PUS9235 that it plays and reports film time. The placeholder is
    never fetched (a discontinuity follows)."""
    if base <= _GAP_MIN_S:
        return text
    out: list[str] = []
    placed = False
    for line in text.splitlines():
        if line.startswith("#EXT-X-VERSION:"):
            line = "#EXT-X-VERSION:8"  # EXT-X-GAP
        if not placed and (line.startswith("#EXTINF") or (line and not line.startswith("#"))):
            out += ["#EXT-X-GAP", f"#EXTINF:{base:.3f},", "gap.ts", "#EXT-X-DISCONTINUITY"]
            placed = True
        out.append(line)
    return "\n".join(out) + "\n"


def playlist_name(gen: int) -> str:
    return PLAYLIST if gen == 0 else f"g{gen}.m3u8"


def segment_name(gen: int, index: int) -> str:
    return f"index{index}.ts" if gen == 0 else f"g{gen}_{index}.ts"


def is_playlist_name(name: str) -> bool:
    return bool(_PLAYLIST_NAME.fullmatch(name))


_EXTINF = re.compile(r"#EXTINF:([0-9.]+)")


@dataclass(frozen=True)
class Job:
    """What to produce: the source url, the audio mapping/encoding (ffmpeg args) and the
    initial play head `head_s` (the resume point the LOAD will seek to: the pacing must let
    the producer reach it, and nothing before it needs to stay on disk). `ss_s` > 0 starts
    the producer there instead (fast resume): the playlist then begins at that point and
    the receiver counts from 0 — callers add `first_pts` to what it reports."""

    url: str
    audio_map: str = "0:a:0?"
    audio_args: tuple[str, ...] = ("-c:a", "aac", "-ac", "2", "-b:a", "192k")
    head_s: float = 0.0
    ss_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "url": self.url, "audio_map": self.audio_map,
            "audio_args": list(self.audio_args), "head_s": self.head_s, "ss_s": self.ss_s,
        }  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict) -> Job:
        return cls(
            str(d["url"]), str(d["audio_map"]),
            tuple(str(a) for a in d["audio_args"]), float(d.get("head_s") or 0.0),
            float(d.get("ss_s") or 0.0),
        )  # fmt: skip


def producer_cmd(src: str, out_dir: str, job: Job, gen: int = 0) -> list[str]:
    """The ffmpeg argv: video copied, one audio track, MPEG-TS segments in a growing EVENT
    playlist. `temp_file` renames each segment and playlist into place, so the server never
    serves a half-written file. `src` must already be token-free (a loopback url).

    With `job.ss_s` the input is opened at that point (keyframe before it, audio aligned to
    it: `-noaccurate_seek`, matrix #12) and the source timestamps are kept (`-copyts`, no
    mux delay), so `first_pts` reads where the playlist really starts."""
    seek = ["-ss", f"{job.ss_s:.3f}", "-noaccurate_seek"] if job.ss_s > 0 else []
    keep_ts = ["-copyts", "-muxdelay", "0", "-muxpreload", "0"] if job.ss_s > 0 else []
    return [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-rw_timeout", "30000000", *seek, "-i", src,
        "-map", "0:v:0", "-map", job.audio_map, "-c:v", "copy", *job.audio_args, *keep_ts,
        "-f", "hls", "-hls_time", str(SEGMENT_S), "-hls_list_size", "0",
        "-hls_playlist_type", "event", "-hls_segment_type", "mpegts",
        "-hls_flags", "temp_file",
        "-hls_segment_filename", os.path.join(out_dir, segment_name(gen, 0)[:-4] + "%d.ts"),
        os.path.join(out_dir, playlist_name(gen)),
    ]  # fmt: skip


def first_pts(out_dir: str, gen: int = 0) -> float | None:
    """The source time the playlist starts at (first segment's start), or None. With a
    fast-resume `ss_s` it is the keyframe at or before it — the offset between what the
    receiver reports (playlist time, from 0) and the film."""
    proc = util.run_cmd(
        ["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0",
         os.path.join(out_dir, segment_name(gen, 0))],
        timeout=10,
    )  # fmt: skip
    try:
        return float((proc.stdout or "").split()[0]) if proc and proc.returncode == 0 else None
    except (IndexError, ValueError):
        return None


def timeline(out_dir: str, gen: int = 0) -> list[tuple[int, float, float]]:
    """(segment index, start s, duration s) of every segment generation `gen`'s playlist
    lists so far."""
    try:
        with open(os.path.join(out_dir, playlist_name(gen)), encoding="utf-8") as f:
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
            out.append((int(s.group(2)), t, dur))
            t += dur
    return out


def produced_s(out_dir: str, gen: int = 0) -> float:
    """Seconds of media generation `gen`'s playlist already lists."""
    tl = timeline(out_dir, gen)
    return tl[-1][1] + tl[-1][2] if tl else 0.0


def segment_id(name: str) -> tuple[int, int] | None:
    """(generation, index) of a segment file name, or None for anything else."""
    m = _SEGMENT.fullmatch(name)
    return (int(m.group(1) or 0), int(m.group(2))) if m else None


def segment_index(name: str) -> int | None:
    sid = segment_id(name)
    return sid[1] if sid else None


def _spawn(job: Job, out_dir: str, gen: int) -> subprocess.Popen | None:
    cmd = producer_cmd(urlproxy.local_url(job.url), out_dir, job, gen)
    try:
        # stderr beside the segments (never served: the route whitelists names), read
        # back by `failure_reason`, removed with the directory.
        with open(os.path.join(out_dir, "ffmpeg.log"), "ab") as err:
            return subprocess.Popen(  # noqa: S603
                cmd, stdout=subprocess.DEVNULL, stderr=err,
                preexec_fn=util.die_with_parent,  # noqa: PLW1509 — owned by this process
            )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        _log.warning("live: ffmpeg non avviato (%s)", type(e).__name__)
        return None


def _terminate(proc: subprocess.Popen) -> None:
    """End ffmpeg, resuming it first: a stopped process ignores SIGTERM until continued."""
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.send_signal(signal.SIGCONT)
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _remove_generation(out_dir: str, gen: int) -> None:
    with contextlib.suppress(OSError):
        for entry in os.scandir(out_dir):
            sid = segment_id(entry.name)
            if (sid is not None and sid[0] == gen) or entry.name == playlist_name(gen):
                with contextlib.suppress(OSError):
                    os.unlink(entry.path)


@dataclass
class Producer:
    """A running producer and its disk policy. `on_request` is called by the server for
    each segment served; `tick` runs the pacing periodically; `restart` moves it to another
    film time (seek anywhere); `stop` ends it all."""

    out_dir: str
    proc: subprocess.Popen
    newest_s: float = 0.0
    paused: bool = False
    gen: int = 0
    job: Job | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stopped: bool = False
    _pending: tuple[float, int] | None = None

    @classmethod
    def start(cls, job: Job, out_dir: str, gen: int = 0) -> Producer | None:
        proc = _spawn(job, out_dir, gen)
        if proc is None:
            return None
        return cls(out_dir, proc, newest_s=job.head_s, gen=gen, job=job)

    def request_restart(self, ss_s: float, gen: int) -> None:
        """Queue a restart for the pacing thread. ffmpeg is spawned with PR_SET_PDEATHSIG,
        which fires when the spawning THREAD exits: a restart run from a short-lived
        thread (the SIGUSR1 handler's) had its new ffmpeg killed while opening the source
        (field 2026-10-02: "Immediate exit requested")."""
        with self._lock:
            self._pending = (ss_s, gen)

    def restart(self, ss_s: float, gen: int) -> bool:
        """Stop this producer and start generation `gen` at film time `ss_s` (fast-resume
        style: the new playlist counts from 0). The previous generation's files go."""
        if self.job is None or gen <= self.gen:
            return False
        job = dataclasses.replace(self.job, ss_s=max(ss_s, 0.0), head_s=0.0)
        proc = _spawn(job, self.out_dir, gen)
        if proc is None:
            return False
        with self._lock:
            old, old_gen = self.proc, self.gen
            self.proc, self.gen, self.job = proc, gen, job
            self.newest_s, self.paused = 0.0, False
        _terminate(old)
        _remove_generation(self.out_dir, old_gen)
        return True

    def failed(self) -> bool:
        """The producer exited without producing anything usable."""
        return self.proc.poll() not in (None, 0) and not timeline(self.out_dir, self.gen)

    def failure_reason(self) -> str:
        """The tail of ffmpeg's stderr (diagnosis only; it never carries the token, ffmpeg
        only knows the loopback url)."""
        try:
            with open(os.path.join(self.out_dir, "ffmpeg.log"), "rb") as f:
                return f.read()[-300:].decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def on_request(self, name: str) -> None:
        """Segment `name` was requested: advance the play head, prune behind it. Requests
        for another generation (a receiver still on the old playlist) are ignored."""
        sid = segment_id(name)
        if sid is None or sid[0] != self.gen:
            return
        gen, index = sid
        tl = timeline(self.out_dir, gen)
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
                os.unlink(os.path.join(self.out_dir, segment_name(gen, i)))

    def tick(self) -> None:
        """Pause the producer far ahead of the play head, resume it when it catches up."""
        with self._lock:
            proc, gen = self.proc, self.gen
        if proc.poll() is not None:
            return
        ahead = produced_s(self.out_dir, gen) - self.newest_s
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
        """Start the pacing loop in a daemon thread (until `stop`: a restart swaps the
        process underneath it)."""

        def loop() -> None:
            while not self._stopped:
                with self._lock:
                    pending, self._pending = self._pending, None
                if pending is not None:
                    self.restart(*pending)
                self.tick()
                time.sleep(interval if self._pending is None else 0.0)

        t = threading.Thread(target=loop, name="nstream-live-pacing", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        """Terminate ffmpeg and remove the segment directory."""
        self._stopped = True
        _terminate(self.proc)
        shutil.rmtree(self.out_dir, ignore_errors=True)
