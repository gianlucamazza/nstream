"""Unit tests for the OpenSubtitles moviehash (two ranged HTTP reads)."""

from __future__ import annotations

import struct

from nstream import oshash

CHUNK = oshash.CHUNK


def test_checksum64_zero_chunks_is_size():
    """All-zero chunks contribute nothing: hash = size, hex-formatted to 16 digits."""
    assert oshash.checksum64(0x20000, b"\0" * CHUNK, b"\0" * CHUNK) == "0000000000020000"


def test_checksum64_known_words_and_wraparound():
    """Word sums are little-endian uint64 and the total wraps mod 2^64."""
    head = struct.pack("<Q", 1) + b"\0" * (CHUNK - 8)
    tail = struct.pack("<Q", 0xFFFFFFFFFFFFFFFF) + b"\0" * (CHUNK - 8)
    # size 2 + 1 + (2^64 - 1) ≡ 2 (mod 2^64)
    assert oshash.checksum64(2, head, tail) == "0000000000000002"


class _Resp:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body, self.headers = body, headers

    def read(self, n: int) -> bytes:
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_hash_url_two_ranged_reads(monkeypatch):
    size = 3 * CHUNK
    data = bytes(range(256)) * (size // 256)
    calls: list[str] = []

    def fake_urlopen(req, timeout):
        rng = req.headers["Range"]
        calls.append(rng)
        start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        return _Resp(data[start : end + 1], {"Content-Range": f"bytes {start}-{end}/{size}"})

    monkeypatch.setattr(oshash.urllib.request, "urlopen", fake_urlopen)
    got = oshash.hash_url("http://u/v.mkv")
    assert got is not None
    h, s = got
    assert s == size and h == oshash.checksum64(size, data[:CHUNK], data[-CHUNK:])
    assert calls == [f"bytes=0-{CHUNK - 1}", f"bytes={size - CHUNK}-{size - 1}"]


def test_hash_url_refuses_tiny_and_short_reads(monkeypatch):
    monkeypatch.setattr(
        oshash.urllib.request, "urlopen",
        lambda req, timeout: _Resp(b"\0" * 100, {"Content-Range": f"bytes 0-99/{100}"}),
    )  # fmt: skip
    assert oshash.hash_url("http://u/tiny.mkv") is None


def test_hash_url_network_error_returns_none(monkeypatch):
    def boom(req, timeout):
        raise OSError("no route")

    monkeypatch.setattr(oshash.urllib.request, "urlopen", boom)
    assert oshash.hash_url("http://u/v.mkv") is None
    assert oshash.hash_url("not-a-url") is None  # non-http refused outright
