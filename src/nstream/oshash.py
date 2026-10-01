"""OpenSubtitles moviehash (OSHash) of a remote stream, via two ranged HTTP reads.

The hash identifies the EXACT file being played: `filesize + 64-bit little-endian
checksum of the first and last 64 KB` (mod 2^64), formatted as 16 lowercase hex digits.
Sent to a subtitles addon as the `videoHash` extra it returns tracks timed for this very
release — synchronized by construction, no release-name guessing (ADR 0018).

Remote-friendly: a debrid/HTTP stream needs only two `Range` GETs of 64 KB each (the
size comes from the first response's `Content-Range`), so hashing costs ~128 KB before
playback. Best-effort like the rest of the leaf modules: any failure → None, never
raises, never logs the URL (it may embed the debrid token).
"""

from __future__ import annotations

import contextlib
import struct
import urllib.error
import urllib.request

from . import log
from .net import UA

_log = log.get_logger("oshash")

CHUNK = 65536  # 64 KB, per the OSHash spec
_TIMEOUT = 15.0
# Real releases are never this small; refusing keeps the two-read model simple (the
# spec hashes overlapping chunks below 128 KB, a case that can't matter for video).
_MIN_SIZE = 2 * CHUNK


def checksum64(size: int, head: bytes, tail: bytes) -> str:
    """Pure OSHash from the parts: size + Σ uint64-LE(head) + Σ uint64-LE(tail), mod 2^64.
    `head`/`tail` must be exactly 64 KB each (the caller guarantees it)."""
    total = size
    for chunk in (head, tail):
        total += sum(struct.unpack(f"<{CHUNK // 8}Q", chunk))
    return f"{total & 0xFFFFFFFFFFFFFFFF:016x}"


def _ranged_read(url: str, start: int, end: int) -> tuple[bytes, int]:
    """GET `bytes=start-end` of `url` → (body, total_size). Total size comes from
    `Content-Range: bytes a-b/TOTAL` (0 when the server answered 200 without one —
    the body is then the whole file and the caller sizes it from Content-Length)."""
    # Same UA as every other nstream fetch: debrid CDNs reject the urllib default (403).
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        body = resp.read(end - start + 1)
        total = 0
        crange = resp.headers.get("Content-Range") or ""
        if "/" in crange:
            with contextlib.suppress(ValueError):
                total = int(crange.rsplit("/", 1)[1])
        if not total:
            with contextlib.suppress(TypeError, ValueError):
                total = int(resp.headers.get("Content-Length") or 0)
        return body, total


@log.phase("oshash")
def hash_url(url: str) -> tuple[str, int] | None:
    """OSHash of the file behind `url` → (hash, size), or None when it can't be computed
    (no Range support, tiny file, network error). Two 64 KB reads, ~128 KB total."""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        head, size = _ranged_read(url, 0, CHUNK - 1)
        if len(head) < CHUNK or size < _MIN_SIZE:
            return None
        tail, _ = _ranged_read(url, size - CHUNK, size - 1)
        if len(tail) < CHUNK:
            return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log.info("oshash non calcolabile: %s", e.__class__.__name__)
        return None
    return checksum64(size, head, tail), size
