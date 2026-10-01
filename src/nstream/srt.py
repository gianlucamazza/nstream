"""Subtitle text-format toolbox: decode, parse cues, retime, SRT→WebVTT.

The single owner of subtitle TEXT concerns, shared by three consumers: the selection
orchestrator (`subs`), the alignment engine (`subalign`) and the delivery tier
(`caster`/`remux` need only `to_vtt` — they must not depend on the orchestrator).
Top-tier leaf: imports nothing from nstream (like `languages`).

Every operation goes through one cue model (`parse_cues` → `Cue` → `write_srt` /
`write_vtt`), so retiming, conversion and alignment read a subtitle the same way.

Encoding doctrine (ADR 0018, field lesson): provider `SubEncoding` metadata lies and
`errors="replace"` mojibakes every accented character — decode UTF-8 (BOM-aware) first,
then a UTF-16 BOM, then CP1252 (curly quotes and ellipses live in 0x80–0x9F, which latin-1
turns into invisible control characters), latin-1 last (every byte decodes). Always write
UTF-8 (the WebVTT spec REQUIRES it; mpv is happiest with it too).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A cue-timing timestamp (`HH:MM:SS,mmm`; `.` accepted, 1–3 millisecond digits).
_TS = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
# ASS/SSA override blocks some SRTs carry ({\an8}, {\i1}): shown literally on a TV.
_ASS_TAG = re.compile(r"\{\\[^}]*\}")
_FONT_TAG = re.compile(r"</?font[^>]*>", re.I)
# VTT allows only a few inline tags; any other "<" starts text, not markup.
_VTT_TAG = re.compile(r"</?(?:i|b|u)>", re.I)


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    lines: tuple[str, ...]


def decode(path: str) -> str | None:
    """Decode a subtitle file WITHOUT destroying accents (see module doc). None on read
    failure."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _ts_s(groups: tuple[str, str, str, str]) -> float:
    h, mn, s, ms = groups
    return int(h) * 3600 + int(mn) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def _fmt_ts(t: float, sep: str = ",") -> str:
    t = max(t, 0.0)
    whole, milli = divmod(round(t * 1000), 1000)
    mm, ss = divmod(whole, 60)
    hh, mm = divmod(mm, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{milli:03d}"


def parse_cues(text: str) -> list[Cue]:
    """Every well-formed cue, in file order. A block is the lines between blank lines
    (whitespace-only lines count as blank); its timing line is the first one carrying
    `-->` with two timestamps. Malformed blocks are skipped, never guessed."""
    cues: list[Cue] = []
    block: list[str] = []
    for raw in [*text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), ""]:
        line = raw.rstrip()
        if line.strip():
            block.append(line)
            continue
        if block:
            cue = _parse_block(block)
            if cue is not None:
                cues.append(cue)
            block = []
    return cues


def _parse_block(block: list[str]) -> Cue | None:
    for i, line in enumerate(block):
        if "-->" not in line:
            continue
        ts = _TS.findall(line)
        if len(ts) < 2:
            return None
        start, end = _ts_s(ts[0]), _ts_s(ts[1])
        return Cue(start, end, tuple(block[i + 1 :])) if end > start else None
    return None


def write_srt(cues: list[Cue]) -> str:
    out = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{_fmt_ts(c.start)} --> {_fmt_ts(c.end)}\n" + "\n".join(c.lines))
    return "\n\n".join(out) + "\n"


def _vtt_text(line: str) -> str:
    line = _FONT_TAG.sub("", _ASS_TAG.sub("", line)).replace("-->", "→")
    line = re.sub(r"&(?!(?:[a-zA-Z]+|#\d+);)", "&amp;", line)
    # Escape every "<" that doesn't open an allowed VTT tag ("<3" would swallow the cue).
    parts = _VTT_TAG.split(line)
    tags = _VTT_TAG.findall(line)
    out = parts[0].replace("<", "&lt;")
    for tag, rest in zip(tags, parts[1:], strict=True):
        out += tag.lower() + rest.replace("<", "&lt;")
    return out


def write_vtt(cues: list[Cue]) -> str:
    out = ["WEBVTT", ""]
    for c in cues:
        text = [t for t in (_vtt_text(ln) for ln in c.lines) if t.strip()]
        out.append(f"{_fmt_ts(c.start, '.')} --> {_fmt_ts(c.end, '.')}")
        out.extend(text or [""])
        out.append("")
    return "\n".join(out)


def retime(path: str, offset: float, scale: float) -> bool:
    """Retime an SRT in place: t' = t * scale + offset. A cue that ends at or before 0 is
    DROPPED (clamping it piled every earlier line onto 00:00:00 — 1018 cues on a −4800 s
    shift, 2026-10-01); one straddling 0 keeps its visible part. Applied upstream of every
    delivery path, so mpv and the cast's WebVTT see identical timings. Output is UTF-8.
    False on I/O failure."""
    text = decode(path)
    if text is None:
        return False
    shifted = [
        Cue(max(c.start * scale + offset, 0.0), c.end * scale + offset, c.lines)
        for c in parse_cues(text)
    ]
    kept = [c for c in shifted if c.end > 0 and c.end > c.start]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(write_srt(kept))
    except OSError:
        return False
    return True


def to_vtt(srt_path: str) -> str | None:
    """Convert an SRT file to WebVTT, required for a side-loaded Cast caption track.
    Writes a sibling `<name>.vtt` and returns its path (or None on failure). Cues are
    re-emitted from the parsed model: 3-digit milliseconds, ASS/`<font>` tags removed,
    stray `<`/`&` escaped, `-->` in text neutralized — any of which makes a receiver
    drop or garble a cue. A file already starting with `WEBVTT` is copied through."""
    text = decode(srt_path)
    if text is None:
        return None
    out = text if text.lstrip().startswith("WEBVTT") else write_vtt(parse_cues(text))
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
    alignment engine's view of a subtitle. An unreadable or cue-less file yields ()."""
    text = decode(path)
    if text is None:
        return ()
    return tuple((c.start, c.end) for c in parse_cues(text))
