"""Subtitle text-format toolbox: decode, retime, SRT→WebVTT, cue parsing.

The single owner of subtitle TEXT concerns, shared by three consumers: the selection
orchestrator (`subs`), the alignment engine (`subalign`) and the delivery tier
(`caster`/`remux` need only `to_vtt` — they must not depend on the orchestrator).
Top-tier leaf: imports nothing from nstream (like `languages`).

Encoding doctrine (ADR 0018, field lesson): provider `SubEncoding` metadata lies and
`errors="replace"` mojibakes every accented character — decode UTF-8 (BOM-aware) first,
fall back to latin-1 (total: every byte decodes), and always write UTF-8 (the WebVTT
spec REQUIRES it; mpv is happiest with it too).
"""

from __future__ import annotations

import re

# The canonical SRT cue-timing timestamp (`HH:MM:SS,mmm`, `.` accepted as separator).
# Only lines containing `-->` are ever rewritten; cue ids and text stay untouched.
_TS = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def decode(path: str) -> str | None:
    """Decode a subtitle file WITHOUT destroying accents: UTF-8 (BOM-aware) first, then
    latin-1 for the common CP1252/latin-1 tracks. None on read failure."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _ts_s(m: re.Match[str]) -> float:
    h, mn, s, ms = m.groups()
    return int(h) * 3600 + int(mn) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _fmt_ts(t: float) -> str:
    t = max(t, 0.0)
    whole, milli = divmod(round(t * 1000), 1000)
    mm, ss = divmod(whole, 60)
    hh, mm = divmod(mm, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{milli:03d}"


def retime(path: str, offset: float, scale: float) -> bool:
    """Retime an SRT in place: t' = t * scale + offset (clamped at 0), on cue-timing
    lines only. Applied upstream of BOTH delivery paths, so mpv and the cast's WebVTT
    see identical corrected timings. Output is always UTF-8. False on I/O failure."""

    def _shift(m: re.Match[str]) -> str:
        return _fmt_ts(_ts_s(m) * scale + offset)

    text = decode(path)
    if text is None:
        return False
    out = "\n".join(_TS.sub(_shift, ln) if "-->" in ln else ln for ln in text.splitlines())
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(out + "\n")
    except OSError:
        return False
    return True


def to_vtt(srt_path: str) -> str | None:
    """Convert an SRT file to WebVTT, required for a side-loaded Cast caption track.
    Writes a sibling `<name>.vtt` and returns its path (or None on failure). Minimal
    and safe: prepend the `WEBVTT` header and turn the `,` millisecond separator into
    `.` on cue-timing lines only (`-->`), leaving cue identifiers and text untouched.
    Always UTF-8 (the WebVTT spec requires it). Idempotent-ish: a file already starting
    with `WEBVTT` is copied through unchanged."""
    text = decode(srt_path)
    if text is None:
        return None
    if text.lstrip().startswith("WEBVTT"):
        out = text
    else:
        lines = ["WEBVTT", ""]
        lines += [ln.replace(",", ".") if "-->" in ln else ln for ln in text.splitlines()]
        out = "\n".join(lines) + "\n"
    base = srt_path[:-4] if srt_path.lower().endswith(".srt") else srt_path
    vtt_path = f"{base}.vtt"
    try:
        with open(vtt_path, "w", encoding="utf-8") as f:
            f.write(out)
    except OSError:
        return None
    return vtt_path


def cue_spans(path: str) -> tuple[tuple[float, float], ...]:
    """The (start_s, end_s) interval of every parsable cue, in file order — the
    alignment engine's view of a subtitle. Malformed blocks are skipped; an unreadable
    or cue-less file yields ()."""
    text = decode(path)
    if text is None:
        return ()
    spans: list[tuple[float, float]] = []
    for ln in text.splitlines():
        if "-->" not in ln:
            continue
        ts = _TS.findall(ln)
        if len(ts) < 2:
            continue

        def _sec(groups: tuple[str, str, str, str]) -> float:
            h, mn, s, ms = groups
            return int(h) * 3600 + int(mn) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000

        start, end = _sec(ts[0]), _sec(ts[1])
        if end > start >= 0:
            spans.append((start, end))
    return tuple(spans)
