"""Source availability: classified probes, dead-source denylist, drop unusable.

Owns ADR 0014/0025/0028 mechanics that decide whether a resolved URL is usable *now* and
whether a release is remembered as gone. Ranking/resolve stay in `stream_select`;
this module is the probe+denylist leaf they call.

Public: `source_key`, `prune_dead`, `probe_stream`, `probe_url`, `drop_unusable`,
`drop_streams`, `expected_bytes`, `vet_duration`, `DurationVerdict`, `VERIFY_CAP`,
`clear_memo`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import log, net, notices, quality, state, tracks, util
from .config import Config
from .types import Stream

_log = log.get_logger("availability")

# Process-lifetime memo: a resolved url's availability does not change within a run.
# Availability changes over a long TUI session (an `[RD download]` becomes live, a link
# dies), so the memo expires; LRU-bounded, thread-safe (probes run on a pool).
_PROBE_MEMO: util.BoundedMemo[str, net.Probe] = util.BoundedMemo(512, ttl=600.0)
VERIFY_CAP = 5  # top-N candidates to probe before committing the auto-pick


def source_key(stream: Stream) -> str:
    """Denylist identity of what a probe of this row would prove gone (ADR 0038, amending
    0025). A row carrying a ready url (a debrid link) keys on the addon that produced the
    link plus the file — a provider's 404 proves *that link* gone, not the torrent, which
    stays playable via P2P or another provider. A pure-torrent row keys on its infoHash.
    Never a display name: those are shared across releases ("[RD+] Torrentio 4k"). Empty
    when the row carries no identity — such a stream is simply never denylisted."""
    ih = (stream.get("infoHash") or "").strip().lower()
    hints = stream.get("behaviorHints") or {}
    filename = (hints.get("filename") or "").strip() if isinstance(hints, dict) else ""
    if stream.get("url"):
        ident = ih or (f"file:{filename}" if filename else "")
        if not ident:
            return ""
        idx = stream.get("fileIdx")
        file_part = filename or (str(idx) if idx is not None else "")
        return f"url:{stream.get('addon') or '?'}:{ident}:{file_part}"
    if ih:
        return ih
    return f"file:{filename}" if filename else ""


def expected_bytes(stream: Stream) -> int:
    """Announced release size in bytes (0 when the name carries no size), used by the probe
    to tell a real file from a placeholder served in place of a removed one."""
    size_gb = quality.parse_stream(stream).size_gb
    return int(size_gb * 1024**3) if size_gb > 0 else 0


def probe_stream(stream: Stream) -> net.Probe:
    """Memoized, classified availability probe for a resolved stream.
    Only a `gone` verdict is persisted: it proves the source isn't there."""
    url = stream.get("url") or ""
    probe = _PROBE_MEMO.get(url)
    if probe is None:
        probe = net.probe_url(url, expected_bytes=expected_bytes(stream))
        _PROBE_MEMO[url] = probe
        if probe.dead:
            remember_dead(stream, probe)
    return probe


def remember_dead(stream: Stream, probe: net.Probe) -> None:
    """Persist a proven-removed source and say so once, on stderr."""
    key = source_key(stream)
    if not key or state.is_dead(key):
        return
    name_line = next(iter((stream.get("name") or "").splitlines()), "") or key
    notices.emit(f"sorgente non più disponibile ({probe.reason}) — {name_line}")
    _log.info("sorgente morta: %s (%s)", key, probe.reason)
    state.mark_dead(key, probe.reason)


def probe_url(url: str) -> bool:
    """Boolean façade for callers that only ask "can I play this now?"."""
    return probe_stream({"url": url}).usable


def prune_dead(cfg: Config, results: list[Stream]) -> tuple[list[Stream], int]:
    """Drop sources previously proven removed (ADR 0025) before ranking.
    No-op for the local backend and when the denylist is empty."""
    if cfg.playback_backend == "local" or not results:
        return results, 0
    dead = state.dead_sources()
    if not dead:
        return results, 0
    kept = [s for s in results if source_key(s) not in dead]
    return kept, len(results) - len(kept)


def demote_cached(stream: Stream) -> None:
    """Strip the debrid cached marker so ranking no longer treats it as instant."""
    name = stream.get("name") or ""
    stripped = quality._CACHED_RE.sub("", name).strip()
    if stripped != name:
        stream["name"] = stripped


def drop_streams(results: list[Stream], bad: list[Stream]) -> list[Stream]:
    """Remove `bad` from `results` **in place**, by identity: two rows can compare equal
    (same release from two addons) while only one is the one just proven unusable."""
    if not bad:
        return results
    dropped = {id(s) for s in bad}
    results[:] = [s for s in results if id(s) not in dropped]
    return results


def drop_unusable(results: list[Stream], targets: list[Stream]) -> list[Stream]:
    """Probe `targets` concurrently; demote and drop any unusable from `results`."""
    if not targets:
        return results
    with ThreadPoolExecutor(max_workers=min(len(targets), VERIFY_CAP)) as ex:
        verdicts = list(ex.map(probe_stream, targets))
    unusable = []
    for s, probe in zip(targets, verdicts, strict=True):
        if not probe.usable:
            demote_cached(s)
            unusable.append(s)
    return drop_streams(results, unusable)


# --- content duration vetting (ADR 0028) ------------------------------------

MIN_RUNTIME_RATIO = 0.35  # below this fraction of the expected runtime it isn't the video
MIN_EXPECTED_S = 600.0  # never judge shorts/clips: too little room between real and fake


@dataclass(frozen=True)
class DurationVerdict:
    """Outcome of the truncation guard. `ok=False` **only** on a measured, grotesque
    shortfall — every unknown (no expected runtime, unreadable duration) passes."""

    ok: bool
    duration: float = 0.0  # measured seconds (0 = unreadable)
    expected: float = 0.0  # expected seconds (0 = unknown)
    reason: str = ""


def _fmt_s(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def vet_duration(url: str, expected_s: float) -> DurationVerdict:
    """Truncation guard (ADR 0028): a placeholder served in place of a removed release
    (or a sample inside a pack) lasts a small fraction of the title's runtime.

    Reads the SAME memoized ffprobe the audio/cast vetting already runs
    (`tracks.probe_tracks`), so every call site that vets audio pays nothing extra. This
    is the backend-agnostic twin of the ADR 0025 size check, which can only speak for
    HTTP sources: the duration comes from the file, not from the transport.

    One-way by design — only *too short* is a verdict. An extended cut, a double episode
    or a season pack with the wrong `fileIdx` are all *longer* than expected."""
    if expected_s < MIN_EXPECTED_S or not url:
        return DurationVerdict(True, expected=expected_s)
    duration = tracks.probe_tracks(url).duration
    if duration <= 0:
        return DurationVerdict(True, expected=expected_s)  # unreadable → benefit of the doubt
    if duration >= expected_s * MIN_RUNTIME_RATIO:
        return DurationVerdict(True, duration=duration, expected=expected_s)
    reason = f"durata {_fmt_s(duration)} contro ~{int(expected_s // 60)} min attesi"
    return DurationVerdict(False, duration=duration, expected=expected_s, reason=reason)


def clear_memo() -> None:
    """Test seam: the probe memo is process-lifetime."""
    _PROBE_MEMO.clear()
