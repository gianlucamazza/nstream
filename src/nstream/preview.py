"""Body of the hidden `nstream __preview` subcommand: renders the focused title/episode
into the fzf preview pane — a poster thumbnail (via chafa, if the terminal can show one)
plus a formatted metadata card. fzf spawns this as a child process per focused row and
exports FZF_PREVIEW_COLUMNS / FZF_PREVIEW_LINES for sizing.

Everything is best-effort: any failure prints a minimal card or nothing and returns 0,
never a traceback (which would corrupt the pane). Only meta/title data is rendered — never
stream URLs — so no token can leak here.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

from . import api, config, ui, util
from .config import Config

_POSTER_TIMEOUT = 4.0  # short: a slow CDN must never freeze the pane
_CHAFA_TIMEOUT = 5.0

# Poster cache bound: ~200 posters / 50 MB covers weeks of browsing (a poster is
# ~50-300 KB) while keeping $XDG_CACHE_HOME from growing forever. Pruned oldest-first
# (mtime) and only on a cache miss — never on the hot per-row preview path.
_POSTER_CACHE_MAX_FILES = 200
_POSTER_CACHE_MAX_BYTES = 50 * 1024 * 1024


def run_preview(argv: list[str]) -> int:
    """Entry for `__preview`. argv is `title <typ> <id>` or `episode <series_id> <s> <e>`.
    fzf passes the row field as a single shell-quoted arg, so re-split on whitespace (the
    tokens never contain spaces). Always returns 0."""
    with contextlib.suppress(Exception):
        out = _render(" ".join(argv).split())
        if out:
            print(out)
    return 0


def run_layout() -> int:
    """Entry for `__layout`: fzf's resize transform. Re-derives the preview window
    placement for the new terminal size and refreshes the pane so chafa re-renders
    the poster at the new dimensions. Best-effort: prints nothing on failure (fzf
    treats empty transform output as a no-op)."""
    with contextlib.suppress(Exception):
        cols = _int_env("FZF_COLUMNS", 80)
        lines = _int_env("FZF_LINES", 24)
        lay = ui.layout_for(cols, lines, ui.detect_caps(_load_cfg()))
        print(f"change-preview-window({lay.preview_window})+refresh-preview")
    return 0


def _render(argv: list[str]) -> str:
    kind = argv[0] if argv else ""
    cfg = _load_cfg()
    caps = ui.detect_caps(cfg)
    cols, lines = _pane_size()
    if kind == "title" and len(argv) >= 3:
        return _render_title(cfg, argv[1], argv[2], cols, lines, caps)
    if kind == "episode" and len(argv) >= 4:
        with contextlib.suppress(ValueError):
            return _render_episode(cfg, argv[1], int(argv[2]), int(argv[3]), cols, lines, caps)
    return ""


def _load_cfg() -> Config:
    """Real config if present; otherwise a default (Cinemeta still resolves) so the
    preview works even before onboarding."""
    try:
        return config.load()
    except config.ConfigError:
        return Config(torrentio_base="")


def _pane_size() -> tuple[int, int]:
    return _int_env("FZF_PREVIEW_COLUMNS", 60), _int_env("FZF_PREVIEW_LINES", 20)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- rendering --------------------------------------------------------------


def _render_title(
    cfg: Config, typ: str, video_id: str, cols: int, lines: int, caps: ui.Caps
) -> str:
    m = api.meta_cached_disk(cfg, typ, video_id)
    poster = _poster_block(m.get("poster", ""), cols, lines, caps, cfg)
    text = _format_meta(m, caps, text_width=max(20, cols - 1))
    return _combine(poster, text)


def _render_episode(
    cfg: Config, series_id: str, season: int, episode: int, cols: int, lines: int, caps: ui.Caps
) -> str:
    m = api.meta_cached_disk(cfg, "series", series_id)
    ep = _find_episode(m, season, episode)
    url = (ep.get("thumbnail") if ep else "") or m.get("poster", "")
    poster = _poster_block(url, cols, lines, caps, cfg)
    text = _format_episode(m, ep, season, episode, caps, text_width=max(20, cols - 1))
    return _combine(poster, text)


def _find_episode(m: dict, season: int, episode: int) -> dict | None:
    for v in m.get("videos", []):
        if v.get("season") == season and v.get("episode") == episode:
            return v
    return None


def _format_meta(m: dict, caps: ui.Caps, *, text_width: int) -> str:
    g = ui.glyphs(caps)
    pal = ui.palette(caps)
    lines = [ui.ansi(m.get("name") or "?", pal.accent)]
    bits = []
    if rating := m.get("imdbRating"):
        bits.append(f"{g.star} {rating}")
    if year := _year(m):
        bits.append(year)
    if runtime := m.get("runtime"):
        bits.append(f"{g.clock} {runtime}")
    if bits:
        lines.append(ui.ansi("  ·  ".join(bits), pal.secondary))
    if genres := m.get("genres"):
        lines.append(ui.ansi(" · ".join(genres[:4]), pal.dim))
    if credits := _credits(m, g):
        lines.append(ui.ansi(credits, pal.dim))
    if desc := (m.get("description") or "").strip():
        lines.append("")
        lines.append(textwrap.fill(desc, width=text_width))
    return "\n".join(lines)


def _format_episode(
    m: dict, ep: dict | None, season: int, episode: int, caps: ui.Caps, *, text_width: int
) -> str:
    g = ui.glyphs(caps)
    pal = ui.palette(caps)
    lines = [ui.ansi(m.get("name") or "?", pal.accent)]
    tag = f"S{season:02d}E{episode:02d}"
    name = (ep or {}).get("name") or ""
    lines.append(ui.ansi(f"{g.series} {tag}  {name}".rstrip(), pal.secondary))
    if ep and (aired := _date(ep.get("released"))):
        lines.append(ui.ansi(f"{g.calendar} {aired}", pal.dim))
    overview = ((ep or {}).get("overview") or m.get("description") or "").strip()
    if overview:
        lines.append("")
        lines.append(textwrap.fill(overview, width=text_width))
    return "\n".join(lines)


def _credits(m: dict, g: ui.Glyphs) -> str:
    parts = []
    cast = m.get("cast")
    if isinstance(cast, list) and cast:
        parts.append(f"{g.people} " + ", ".join(str(c) for c in cast[:3]))
    director = m.get("director")
    if isinstance(director, list):
        director = ", ".join(str(d) for d in director)
    if director:
        parts.append(str(director))
    return "  ·  ".join(parts)


def _year(m: dict) -> str:
    for key in ("year", "releaseInfo", "released"):
        if val := m.get(key):
            digits = "".join(c for c in str(val)[:4] if c.isdigit())
            if len(digits) == 4:
                return digits
    return ""


def _date(value: object) -> str:
    return str(value)[:10] if value else ""


def _combine(poster: str, text: str) -> str:
    return f"{poster}\n{text}" if poster else text


# --- poster image (chafa) ---------------------------------------------------


def _poster_block(url: str, cols: int, lines: int, caps: ui.Caps, cfg: Config) -> str:
    """Render the poster as a terminal image escape, or "" when not possible (no URL,
    posters disabled, no image protocol, download/render failure)."""
    if not url or not cfg.posters or caps.image_proto is ui.ImageProto.NONE:
        return ""
    path = _cached_poster(url)
    if not path:
        return ""
    img_lines = min(max(lines - 8, 6), 20)  # leave room for the text card below
    cmd = [
        "chafa", "--format", caps.image_proto.value,
        "--size", f"{cols}x{img_lines}",
        "--animate", "off", "--polite", "on",
    ]  # fmt: skip
    if not caps.truecolor:
        cmd += ["--colors", "256"]
    if caps.image_proto is ui.ImageProto.SYMBOLS:
        cmd += ["--symbols", "block+border+space"]
    cmd.append(str(path))
    proc = util.run_cmd(cmd, timeout=_CHAFA_TIMEOUT)
    return proc.stdout if proc and proc.returncode == 0 else ""


def _poster_cache_path(url: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    h = hashlib.sha256(url.encode()).hexdigest()
    return Path(base) / "nstream" / "posters" / h


def _cached_poster(url: str) -> Path | None:
    """Path to the cached poster bytes, downloading on first use. Posters are immutable
    per URL, so entries never expire — the cache is instead size-bounded by a prune on
    each new write (see `_prune_posters`)."""
    path = _poster_cache_path(url)
    if path.exists():
        return path
    data = _download(url)
    if not data:
        return None
    with contextlib.suppress(OSError):
        util.atomic_write_bytes(path, data, prefix=".poster-")
        with contextlib.suppress(Exception):  # a failed prune must never cost the pane
            _prune_posters(path.parent)
        return path
    return None


def _prune_posters(cache_dir: Path) -> None:
    """Best-effort: evict the oldest posters (by mtime) until the cache is back under
    `_POSTER_CACHE_MAX_FILES` / `_POSTER_CACHE_MAX_BYTES`. Called only after writing a
    new poster (cache miss), so cache hits — the per-row hot path — pay nothing."""
    entries = []
    with contextlib.suppress(OSError):
        for p in cache_dir.iterdir():
            with contextlib.suppress(OSError):
                if p.is_file():
                    entries.append((p, p.stat()))
    count = len(entries)
    total = sum(st.st_size for _, st in entries)
    if count <= _POSTER_CACHE_MAX_FILES and total <= _POSTER_CACHE_MAX_BYTES:
        return
    entries.sort(key=lambda e: e[1].st_mtime)  # oldest first; the just-written file is last
    for p, st in entries:
        if count <= _POSTER_CACHE_MAX_FILES and total <= _POSTER_CACHE_MAX_BYTES:
            break
        with contextlib.suppress(OSError):
            p.unlink()
            count -= 1
            total -= st.st_size


_POSTER_MAX_BYTES = 5 * 1024 * 1024


def _download(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": api.UA})
    try:
        with urllib.request.urlopen(req, timeout=_POSTER_TIMEOUT) as resp:
            data = resp.read(_POSTER_MAX_BYTES + 1)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    # A poster is tens of KB; anything past the cap is not one (and must not fill memory).
    return data if len(data) <= _POSTER_MAX_BYTES else None
