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
    "NO_COLOR",
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


# --- NO_COLOR (no-color.org) --------------------------------------------------


def test_color_on_by_default(clean_env):
    assert ui.detect_caps(use_cache=False).color is True


@pytest.mark.parametrize("value", ["1", ""])  # presence counts, whatever the value
def test_no_color_disables_color(clean_env, value):
    clean_env.setenv("NO_COLOR", value)
    assert ui.detect_caps(use_cache=False).color is False


def test_no_color_palette_is_monochrome(clean_env):
    clean_env.setenv("NO_COLOR", "1")
    clean_env.setenv("COLORTERM", "truecolor")  # NO_COLOR must win over truecolor
    pal = ui.palette(ui.detect_caps(use_cache=False))
    assert all(getattr(pal, f) == "" for f in ("accent", "secondary", "dim", "good", "warn"))
    assert ui.ansi("x", pal.accent) == "x"  # empty SGR → ansi() emits no escapes


def test_no_color_fzf_theme_is_bw(clean_env):
    clean_env.setenv("NO_COLOR", "1")
    assert ui.fzf_color_arg(ui.detect_caps(use_cache=False)) == ["--color", "bw"]


def test_no_color_leaves_glyphs_and_layout_alone(clean_env):
    clean_env.setenv("NO_COLOR", "1")
    clean_env.setenv("TERM", "foot")
    caps = ui.detect_caps(use_cache=False)
    assert ui.glyphs(caps) is ui.PORTABLE  # glyph set untouched
    assert caps.image_proto is ui.ImageProto.SIXEL  # posters untouched
    assert ui.layout_for(120, 40, caps).show_poster is True


def test_no_color_invalidates_caps_cache(clean_env):
    clean_env.setenv("TERM", "foot")
    assert ui.detect_caps().color is True  # caches color=True
    clean_env.setenv("NO_COLOR", "1")  # presence changes the signature → re-detect
    assert ui.detect_caps().color is False


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


def test_glyphs_new_slots_portable_match_legacy_emoji():
    """The 6 slots added for the hardcoded-glyph sweep: PORTABLE must equal the emoji
    previously hardcoded at the call sites, so default output doesn't change."""
    legacy = {"audio": "🔊", "subs": "💬", "globe": "🌐", "tv": "📺", "fail": "✗", "down": "↓"}
    for slot, emoji in legacy.items():
        assert getattr(ui.PORTABLE, slot) == emoji
        assert getattr(ui.NERD, slot)  # present and non-empty in the Nerd Font set too
    assert ui.NERD.tv != ui.NERD.series  # distinct icons in nerd mode


def test_g_helper_follows_active_caps(monkeypatch):
    monkeypatch.setattr(ui, "_active", ui.Caps(nerd_font=True))
    assert ui.g() is ui.NERD
    monkeypatch.setattr(ui, "_active", ui.Caps(nerd_font=False))
    assert ui.g() is ui.PORTABLE


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
