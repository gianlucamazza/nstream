"""Centralized TUI design system: terminal capability detection, the colour palette,
the glyph set (Nerd Font with a portable fallback), the fzf colour theme, ANSI helpers,
progress bars, and the small-terminal layout breakpoints.

Sits near the top of the import graph (only `util`/`config` + stdlib) so both `picker`
and the `preview` subcommand can share one source of truth without an import cycle. It
is pure presentation + detection — it must never import `api`, `picker`, `cli`,
`quality`, or `caster`.
"""

from __future__ import annotations

import contextlib
import enum
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import util
from .config import Config

_CACHE_VERSION = 1

# Terminals we treat as sixel-capable (sixel support isn't reliably env-detectable, so
# this is an allowlist; NSTREAM_IMAGE_PROTO / image_mode let users override either way).
_SIXEL_TERMS = {"foot", "foot-extra", "contour", "yaft-256color"}
_SIXEL_TERM_PROGS = {"WezTerm"}


class ImageProto(enum.Enum):
    """How (and whether) poster thumbnails can be drawn in the fzf preview pane.

    All non-NONE protocols render *through* chafa (which emits the right escapes), so
    they collapse to NONE when chafa is absent. SYMBOLS = ANSI half-blocks, the always-
    safe fallback on any 256/truecolor terminal."""

    NONE = "none"
    SYMBOLS = "symbols"
    SIXEL = "sixel"
    KITTY = "kitty"
    ITERM = "iterm"


@dataclass(frozen=True)
class Caps:
    nerd_font: bool = False
    truecolor: bool = False
    image_proto: ImageProto = ImageProto.NONE
    has_chafa: bool = False


# --- capability detection (cached, mirrors quality.detect_caps) -------------


def _cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "nstream" / "uicaps.json"


def _signature(cfg: Config | None) -> str:
    """Environment + config inputs that determine caps; a change re-triggers detection."""
    env = os.environ
    return "|".join(
        [
            env.get("TERM", ""),
            env.get("COLORTERM", ""),
            env.get("TERM_PROGRAM", ""),
            env.get("KITTY_WINDOW_ID", ""),
            env.get("LC_TERMINAL", ""),
            env.get("NSTREAM_IMAGE_PROTO", ""),
            env.get("NSTREAM_NERD_FONT", ""),
            "1" if shutil.which("chafa") else "0",
            cfg.nerd_font if cfg else "auto",
            cfg.image_mode if cfg else "auto",
        ]
    )


def _resolve_nerd(cfg: Config | None) -> bool:
    pref = cfg.nerd_font if cfg else "auto"
    if pref == "on":
        return True
    if pref == "off":
        return False
    # Nerd Font presence can't be probed; opt in via env, otherwise portable unicode.
    return bool(os.environ.get("NSTREAM_NERD_FONT"))


def _resolve_proto(cfg: Config | None) -> ImageProto:
    env = os.environ
    if cfg and cfg.image_mode == "off":
        return ImageProto.NONE
    forced = env.get("NSTREAM_IMAGE_PROTO", "").strip().lower()
    if forced:
        with contextlib.suppress(ValueError):
            return ImageProto(forced)
    term = env.get("TERM", "")
    term_prog = env.get("TERM_PROGRAM", "")
    if term == "xterm-kitty" or env.get("KITTY_WINDOW_ID"):
        # kitty graphics misbehave inside fzf's scrolling preview pane → use half-blocks
        # unless the user explicitly forces kitty via NSTREAM_IMAGE_PROTO (handled above).
        return ImageProto.SYMBOLS
    if term_prog == "iTerm.app" or env.get("LC_TERMINAL") == "iTerm2":
        return ImageProto.ITERM
    if "sixel" in term or term in _SIXEL_TERMS or term_prog in _SIXEL_TERM_PROGS:
        return ImageProto.SIXEL
    return ImageProto.SYMBOLS


def _detect(cfg: Config | None) -> Caps:
    has_chafa = shutil.which("chafa") is not None
    proto = _resolve_proto(cfg)
    if not has_chafa:  # every protocol needs chafa to emit the escapes
        proto = ImageProto.NONE
    return Caps(
        nerd_font=_resolve_nerd(cfg),
        truecolor=os.environ.get("COLORTERM") in {"truecolor", "24bit"},
        image_proto=proto,
        has_chafa=has_chafa,
    )


def detect_caps(cfg: Config | None = None, *, use_cache: bool = True) -> Caps:
    """Detect terminal capabilities (Nerd Font, truecolor, image protocol, chafa),
    cached on disk and keyed by a signature of the relevant env + config so a terminal
    or setting change re-detects. Best-effort: cache failures never raise."""
    path = _cache_path()
    sig = _signature(cfg)
    if use_cache:
        data = util.load_json(path, {})
        with contextlib.suppress(KeyError, TypeError, ValueError):
            if data.get("version") == _CACHE_VERSION and data.get("sig") == sig:
                return Caps(
                    nerd_font=bool(data["nerd_font"]),
                    truecolor=bool(data["truecolor"]),
                    image_proto=ImageProto(data["image_proto"]),
                    has_chafa=bool(data["has_chafa"]),
                )

    caps = _detect(cfg)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": _CACHE_VERSION,
                    "sig": sig,
                    "nerd_font": caps.nerd_font,
                    "truecolor": caps.truecolor,
                    "image_proto": caps.image_proto.value,
                    "has_chafa": caps.has_chafa,
                }
            )
        )
    return caps


def clear_caps_cache() -> None:
    """Remove the caps cache (used by tests and a settings change)."""
    with contextlib.suppress(OSError):
        _cache_path().unlink()


_active: Caps | None = None


def set_active_caps(caps: Caps) -> None:
    """Pin the caps resolved (with config) once per run, so the picker and label builders
    share them without re-detecting (and without thrashing the cfg-aware disk cache)."""
    global _active
    _active = caps


def active_caps() -> Caps:
    """The caps pinned by `set_active_caps`, or a config-less detection as a fallback."""
    return _active if _active is not None else detect_caps()


# --- glyphs -----------------------------------------------------------------


@dataclass(frozen=True)
class Glyphs:
    star: str
    play: str
    movie: str
    series: str
    search: str
    fire: str
    new: str
    gear: str
    cast: str
    warn: str
    cached: str
    audio: str
    subs: str
    globe: str
    tv: str
    fail: str
    down: str
    clock: str
    calendar: str
    people: str
    folder: str
    add: str
    bar_full: str
    bar_empty: str


# Nerd Font variant uses classic FontAwesome codepoints present in every patched font.
NERD = Glyphs(
    star="",
    play="",
    movie="",
    series="",
    search="",
    fire="",
    new="",
    gear="",
    cast="",
    warn="",
    cached="",
    audio="",
    subs="",
    globe="",
    tv="",
    fail="",
    down="",
    clock="",
    calendar="",
    people="",
    folder="",
    add="",
    bar_full="█",
    bar_empty="░",
)

# Portable variant keeps nstream's existing emoji/unicode so current users see no change.
PORTABLE = Glyphs(
    star="★",
    play="▶",
    movie="🎬",
    series="📺",
    search="🔍",
    fire="🔥",
    new="🆕",
    gear="⚙",
    cast="📡",
    warn="⚠",
    cached="✓",
    audio="🔊",
    subs="💬",
    globe="🌐",
    tv="📺",
    fail="✗",
    down="↓",
    clock="⏱",
    calendar="📅",
    people="👥",
    folder="📁",
    add="➕",
    bar_full="█",
    bar_empty="░",
)


def glyphs(caps: Caps) -> Glyphs:
    return NERD if caps.nerd_font else PORTABLE


def g() -> Glyphs:
    """Glyph set for the active caps — the one-call helper for non-TUI modules
    (engine/remux/caster/…) that only need a glyph in a printed message."""
    return glyphs(active_caps())


# --- colour palette + fzf theme ---------------------------------------------


@dataclass(frozen=True)
class Palette:
    """SGR parameter strings (the bit between ESC[ and m) for the accent colours."""

    accent: str  # Netflix red
    secondary: str  # bright text
    dim: str  # muted grey
    good: str  # green
    warn: str  # amber


_TRUECOLOR = Palette(
    accent="38;2;229;9;20",
    secondary="38;2;245;245;245",
    dim="38;5;244",
    good="38;2;46;204;113",
    warn="38;2;241;196;15",
)
_256 = Palette(
    accent="38;5;196",
    secondary="38;5;255",
    dim="38;5;244",
    good="38;5;42",
    warn="38;5;214",
)


def palette(caps: Caps) -> Palette:
    return _TRUECOLOR if caps.truecolor else _256


# fzf interprets hex in --color and downgrades to 256 itself, so the spec is constant.
_FZF_COLOR_SPEC = (
    "fg:-1,bg:-1,gutter:-1,hl:#e50914,fg+:#ffffff,bg+:#1a1a1a,hl+:#e50914,"
    "pointer:#e50914,marker:#e50914,prompt:#e50914,header:#808080,info:#5f5f5f,"
    "border:#303030,spinner:#e50914,query:#ffffff"
)


def fzf_color_arg(caps: Caps) -> list[str]:
    """The `--color` flag pair giving fzf the Netflix-red-on-dark theme. `caps` is
    accepted for symmetry/future tuning; the hex spec works on any colour depth."""
    return ["--color", _FZF_COLOR_SPEC]


def ansi(text: str, sgr: str) -> str:
    """Wrap `text` in an SGR escape (no-op if `sgr` is empty). Used only in the visible
    label field — never in the hidden index/preview fields fzf splits on."""
    return f"\x1b[{sgr}m{text}\x1b[0m" if sgr else text


def progress_bar(position: float, duration: float, *, width: int, caps: Caps) -> str:
    """A `████░░░░` bar `width` cells wide; empty string when duration is unknown."""
    if duration <= 0 or width <= 0:
        return ""
    g = glyphs(caps)
    frac = max(0.0, min(position / duration, 1.0))
    filled = round(frac * width)
    return g.bar_full * filled + g.bar_empty * (width - filled)


# --- small-terminal layout --------------------------------------------------


@dataclass(frozen=True)
class Layout:
    preview_window: str  # the exact --preview-window value
    show_poster: bool
    text_width: int  # advisory wrap width (the preview process re-reads the real dims)


def layout_for(cols: int, lines: int, caps: Caps) -> Layout:
    """Pick the preview placement, poster on/off, and text width for the current
    terminal size. Narrow terminals drop the poster and split vertically; tiny ones
    hide the preview entirely (still toggleable with ctrl-/)."""
    can_image = caps.image_proto is not ImageProto.NONE
    if lines < 20 or cols < 50:
        return Layout("hidden", False, max(20, cols - 4))
    if cols < 80:
        return Layout("down:45%:wrap", False, max(20, cols - 4))
    if cols < 100:
        return Layout("right:45%:wrap", can_image, max(24, int(cols * 0.55) - 4))
    return Layout("right:50%:wrap", can_image, max(28, cols // 2 - 4))
