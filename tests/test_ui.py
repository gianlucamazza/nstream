"""Unit tests for the TUI design system: capability detection (cached, env-driven),
the colour/glyph selectors, progress bars, and the small-terminal layout breakpoints."""

from __future__ import annotations

import pytest

from nstream import ui
from nstream.config import Config

_ENV_KEYS = (
    "TERM",
    "COLORTERM",
    "TERM_PROGRAM",
    "KITTY_WINDOW_ID",
    "LC_TERMINAL",
    "NSTREAM_IMAGE_PROTO",
    "NSTREAM_NERD_FONT",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate detection from the host: blank the relevant env vars, point the caps
    cache at a tmp dir, and default chafa to present (tests override as needed)."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(ui.shutil, "which", lambda name: "/usr/bin/chafa")
    return monkeypatch


# --- image protocol detection ----------------------------------------------


def test_no_chafa_means_no_image(clean_env):
    clean_env.setattr(ui.shutil, "which", lambda name: None)
    clean_env.setenv("TERM", "foot")
    caps = ui.detect_caps(use_cache=False)
    assert caps.image_proto is ui.ImageProto.NONE
    assert caps.has_chafa is False


def test_kitty_downgrades_to_symbols(clean_env):
    # kitty graphics are unreliable inside fzf → half-blocks unless explicitly forced.
    clean_env.setenv("TERM", "xterm-kitty")
    assert ui.detect_caps(use_cache=False).image_proto is ui.ImageProto.SYMBOLS


def test_forced_kitty_overrides_downgrade(clean_env):
    clean_env.setenv("TERM", "xterm-kitty")
    clean_env.setenv("NSTREAM_IMAGE_PROTO", "kitty")
    assert ui.detect_caps(use_cache=False).image_proto is ui.ImageProto.KITTY


def test_foot_is_sixel(clean_env):
    clean_env.setenv("TERM", "foot")
    assert ui.detect_caps(use_cache=False).image_proto is ui.ImageProto.SIXEL


def test_iterm_detected(clean_env):
    clean_env.setenv("TERM_PROGRAM", "iTerm.app")
    assert ui.detect_caps(use_cache=False).image_proto is ui.ImageProto.ITERM


def test_unknown_term_falls_back_to_symbols(clean_env):
    clean_env.setenv("TERM", "xterm-256color")
    assert ui.detect_caps(use_cache=False).image_proto is ui.ImageProto.SYMBOLS


def test_image_mode_off_forces_none(clean_env):
    clean_env.setenv("TERM", "foot")
    cfg = Config(torrentio_base="tb", image_mode="off")
    assert ui.detect_caps(cfg, use_cache=False).image_proto is ui.ImageProto.NONE


# --- nerd font + truecolor --------------------------------------------------


def test_nerd_font_auto_off_without_env(clean_env):
    assert ui.detect_caps(use_cache=False).nerd_font is False


def test_nerd_font_auto_on_via_env(clean_env):
    clean_env.setenv("NSTREAM_NERD_FONT", "1")
    assert ui.detect_caps(use_cache=False).nerd_font is True


def test_nerd_font_config_on_wins(clean_env):
    cfg = Config(torrentio_base="tb", nerd_font="on")
    assert ui.detect_caps(cfg, use_cache=False).nerd_font is True


def test_nerd_font_config_off_wins_over_env(clean_env):
    clean_env.setenv("NSTREAM_NERD_FONT", "1")
    cfg = Config(torrentio_base="tb", nerd_font="off")
    assert ui.detect_caps(cfg, use_cache=False).nerd_font is False


def test_truecolor_from_colorterm(clean_env):
    clean_env.setenv("COLORTERM", "truecolor")
    assert ui.detect_caps(use_cache=False).truecolor is True


# --- caching ----------------------------------------------------------------


def test_cache_round_trip(clean_env):
    clean_env.setenv("TERM", "foot")
    first = ui.detect_caps()  # writes
    # Flip chafa away; a cached read must still return the written (stale-by-design) value
    # because the signature is unchanged only if which() result is unchanged — so keep it.
    second = ui.detect_caps()
    assert first == second


def test_cache_invalidated_on_signature_change(clean_env):
    clean_env.setenv("TERM", "foot")
    ui.detect_caps()  # caches SIXEL
    clean_env.setattr(ui.shutil, "which", lambda name: None)  # chafa vanished → new sig
    assert ui.detect_caps().image_proto is ui.ImageProto.NONE


# --- palette / glyphs / progress -------------------------------------------


def test_fzf_color_arg_shape(clean_env):
    arg = ui.fzf_color_arg(ui.detect_caps(use_cache=False))
    assert arg[0] == "--color" and "#e50914" in arg[1]


def test_glyphs_nerd_vs_portable():
    assert ui.glyphs(ui.Caps(nerd_font=True)) is ui.NERD
    assert ui.glyphs(ui.Caps(nerd_font=False)) is ui.PORTABLE


def test_ansi_wraps_and_noops():
    assert ui.ansi("x", "31") == "\x1b[31mx\x1b[0m"
    assert ui.ansi("x", "") == "x"


@pytest.mark.parametrize(
    "pos,dur,width,expected_filled",
    [(0, 100, 10, 0), (50, 100, 10, 5), (100, 100, 10, 10), (200, 100, 10, 10)],
)
def test_progress_bar(pos, dur, width, expected_filled):
    bar = ui.progress_bar(pos, dur, width=width, caps=ui.Caps())
    assert bar.count("█") == expected_filled
    assert len(bar) == width


def test_progress_bar_unknown_duration():
    assert ui.progress_bar(10, 0, width=10, caps=ui.Caps()) == ""


# --- layout breakpoints -----------------------------------------------------


@pytest.mark.parametrize(
    "cols,lines,window,poster",
    [
        (120, 40, "right:50%:wrap", True),
        (90, 30, "right:45%:wrap", True),
        (70, 30, "down:45%:wrap", False),
        (120, 15, "hidden", False),
        (40, 40, "hidden", False),
    ],
)
def test_layout_for(cols, lines, window, poster):
    caps = ui.Caps(image_proto=ui.ImageProto.SIXEL)
    lay = ui.layout_for(cols, lines, caps)
    assert lay.preview_window == window
    assert lay.show_poster == poster


def test_layout_no_poster_without_image_proto():
    lay = ui.layout_for(120, 40, ui.Caps(image_proto=ui.ImageProto.NONE))
    assert lay.show_poster is False
    assert lay.preview_window == "right:50%:wrap"
