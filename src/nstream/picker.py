"""fzf-backed list pickers shared by the TUI flow and the cast device/audio menus.

Kept in its own module (importing only `util` + `ui`, never `cli`/`caster`/`preview`) so
both cli and caster can use it without an import cycle. `_run_fzf` is the core; `fzf`
returns just the value, `fzf_key` also reports which key (Enter/Tab/Alt-C) made the
selection. Every menu carries the shared colour theme; title/episode/continue menus can
also opt into a poster+metadata preview pane via the `preview` callback.

Settings and onboarding use `fzf_index` (row labels → selected index) so there is a
single source of fzf chrome — never a parallel `fzf` argv builder.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Callable

from . import ui, util


def _theme_args() -> list[str]:
    """fzf flags applied to every menu: ANSI labels, the Netflix-red colour scheme, and
    the glyph pointer/marker plus rounded chrome."""
    caps = ui.active_caps()
    g = ui.glyphs(caps)
    return [
        "--ansi", "--border", "rounded", "--info", "inline",
        "--pointer", g.play, "--marker", g.cached,
        *ui.fzf_color_arg(caps),
    ]  # fmt: skip


def _preview_exe() -> str:
    """How to re-invoke nstream as the preview child: the installed console script if on
    PATH, else `python -m nstream` for dev/editable checkouts."""
    exe = shutil.which("nstream")
    if exe:
        return shlex.quote(exe)
    return f"{shlex.quote(sys.executable)} -m nstream"


def _preview_args(preview_args: list[str | None]) -> list[str]:
    """fzf flags wiring the per-row preview: the command (field 2 carries the token), the
    size-adaptive window placement, and a toggle key. On terminal resize fzf re-runs
    `nstream __layout` to re-derive the placement and re-render the poster at the new
    size (fzf caches preview output and never re-runs it on its own)."""
    exe = _preview_exe()
    lay = ui.layout_for(*shutil.get_terminal_size((80, 24)), ui.active_caps())
    return [
        "--preview", f"{exe} __preview {{2}}",
        "--preview-window", lay.preview_window,
        "--bind", "ctrl-/:toggle-preview",
        "--bind", f"resize:transform:{exe} __layout",
    ]  # fmt: skip


def _missing_fzf() -> None:
    print("nstream: fzf non trovato", file=sys.stderr)


def _run_fzf[T](
    items: list[tuple[str, T]],
    prompt: str,
    *,
    header: str | None = None,
    expect: tuple[str, ...] = (),
    preview_args: list[str | None] | None = None,
) -> tuple[str, T] | None:
    """Core fzf picker. Returns (key, value): `key` is "" for Enter or one of
    `expect` (e.g. "tab") when that key was pressed; None on ESC/no match.

    With `expect`, fzf prints the pressed key as the first stdout line (empty for
    Enter) before the selection, so the single-item shortcut is skipped to keep the
    alternate key reachable. With `preview_args`, each row gets a hidden field-2 token
    the preview command consumes (None → blank preview for that row)."""
    if not items:
        return None
    if len(items) == 1 and header is None and not expect and preview_args is None:
        return ("", items[0][1])

    # Hidden leading index lets labels repeat without ambiguity. With a preview, a hidden
    # field 2 carries the per-row token (and the visible label moves to field 3..).
    if preview_args is not None:
        lines = "".join(
            f"{i}\t{parg or ''}\t{label}\n"
            for i, ((label, _), parg) in enumerate(zip(items, preview_args, strict=True))
        )
        with_nth = "3.."
    else:
        lines = "".join(f"{i}\t{label}\n" for i, (label, _) in enumerate(items))
        with_nth = "2.."

    # No --height → fzf takes the full alternate screen and restores the terminal on
    # exit, so menus never pile up in the scrollback (clean TUI). --cycle wraps nav.
    cmd = ["fzf", "--prompt", prompt, "--with-nth", with_nth,
           "--delimiter", "\t", "--no-sort", "--reverse", "--cycle",
           *_theme_args()]  # fmt: skip
    if header:
        cmd += ["--header", header]
    if expect:
        cmd += ["--expect", ",".join(expect)]
    if preview_args is not None:
        cmd += _preview_args(preview_args)
    proc = util.run_cmd(cmd, input=lines)
    if proc is None:
        _missing_fzf()
        return None
    if proc.returncode != 0:  # ESC / Ctrl-C / no match
        return None
    out = proc.stdout
    key = ""
    if expect:  # first line is the pressed key (empty = Enter)
        key, _, out = out.partition("\n")
        key = key.strip()
    out = out.strip()
    if not out:
        return None
    return (key, items[int(out.split("\t", 1)[0])][1])


def _map_preview[T](
    items: list[tuple[str, T]], preview: Callable[[T], str | None] | None
) -> list[str | None] | None:
    return [preview(v) for _, v in items] if preview else None


def fzf[T](
    items: list[tuple[str, T]],
    prompt: str,
    *,
    header: str | None = None,
    preview: Callable[[T], str | None] | None = None,
) -> T | None:
    """Pick one of (label, value) pairs via fzf. Returns the value or None.

    `header` shows a transient notice above the list (e.g. why a title couldn't
    play) — it survives the menu reopening, unlike a stderr line that scrolls away.
    `preview(value)` returns the `__preview` token for that row (or None) to show a
    poster+metadata pane. A single item is returned directly only when there's nothing
    to announce or preview."""
    chosen = _run_fzf(items, prompt, header=header, preview_args=_map_preview(items, preview))
    return chosen[1] if chosen else None


def fzf_key[T](
    items: list[tuple[str, T]],
    prompt: str,
    *,
    header: str | None = None,
    expect: tuple[str, ...] = ("tab", "alt-c"),
    preview: Callable[[T], str | None] | None = None,
) -> tuple[str, T] | None:
    """Like `fzf` but also reports which key selected the item — used by the leaf
    title/continue lists where Tab flips auto ↔ manual playback for that pick."""
    return _run_fzf(
        items, prompt, header=header, expect=expect, preview_args=_map_preview(items, preview)
    )


def fzf_multi[T](
    items: list[tuple[str, T]],
    prompt: str,
    *,
    header: str | None = None,
) -> list[T] | None:
    """Multi-select picker: Tab toggles a row's mark, Enter confirms. Returns the marked
    values in display order, or None on ESC / empty result / missing binary. (With nothing
    marked, fzf returns the focused row — callers treat that the same as 'no change'.)"""
    if not items:
        return None
    lines = "".join(f"{i}\t{label}\n" for i, (label, _) in enumerate(items))
    cmd = ["fzf", "--prompt", prompt, "--with-nth", "2..",
           "--delimiter", "\t", "--no-sort", "--reverse", "--cycle",
           "--multi", "--bind", "tab:toggle+down,shift-tab:toggle+up",
           *_theme_args()]  # fmt: skip
    if header:
        cmd += ["--header", header]
    proc = util.run_cmd(cmd, input=lines)
    if proc is None:
        _missing_fzf()
        return None
    if proc.returncode != 0:  # ESC / Ctrl-C / no match
        return None
    chosen = [
        items[int(line.split("\t", 1)[0])][1] for line in proc.stdout.splitlines() if line.strip()
    ]
    return chosen or None


def fzf_index(
    rows: list[str],
    *,
    prompt: str,
    header: str = "",
    help_previews: list[str] | None = None,
) -> int | None:
    """Show plain `rows` in fzf; return the selected index (input order) or None.

    Used by settings/onboarding where the value is the row position itself (enums,
    choice lists). Optional `help_previews` (one line per row) opens a small bottom
    pane with the focused row's help text — same contract the settings menu had."""
    if not rows:
        return None
    lines = "".join(f"{i}\t{r}\n" for i, r in enumerate(rows))
    cmd = [
        "fzf", "--prompt", prompt, "--with-nth", "2..", "--delimiter", "\t",
        "--no-sort", "--reverse", "--cycle", *_theme_args(),
    ]  # fmt: skip
    if header:
        cmd += ["--header", header]
    pv_path: str | None = None
    try:
        if help_previews:
            fd, pv_path = tempfile.mkstemp(prefix="nstream-help-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(p.replace("\n", " ") for p in help_previews))
            cmd += [
                "--preview", f'sed -n "$(({{n}}+1))p" {shlex.quote(pv_path)}',
                "--preview-window", "down:3:wrap",
            ]  # fmt: skip
        proc = util.run_cmd(cmd, input=lines)
        if proc is None:
            _missing_fzf()
            return None
    finally:
        if pv_path:
            with contextlib.suppress(OSError):
                os.unlink(pv_path)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return int(proc.stdout.split("\t", 1)[0])


def confirm(msg: str, *, default_yes: bool = False, non_tty_default: bool = True) -> bool:
    """Yes/No picker that stays inside the fzf chrome. ESC / missing fzf → False on a
    tty; non-interactive (no stdin/stderr tty) returns `non_tty_default` so headless
    callers never hang (remux big-download → proceed; cast confirm uses True too)."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return non_tty_default
    yes_label, no_label = "Sì", "No"
    # Default-first so Enter without moving the cursor matches the stated default.
    items = (
        [(yes_label, True), (no_label, False)]
        if default_yes
        else [(no_label, False), (yes_label, True)]
    )
    header = f"{msg}  ·  INVIO: {'Sì' if default_yes else 'No'} · ESC: annulla"
    chosen = fzf(items, "conferma> ", header=header)
    return False if chosen is None else chosen


def ask_query(prompt: str = "cerca> ") -> str | None:
    """Type a free-text query inside fzf (keeps the alternate-screen chrome). Returns
    the trimmed query, or None on ESC / empty / missing binary.

    Uses a single placeholder row so fzf has something to render; the user types in
    the query box and presses Enter. `--print-query` alone with an empty list exits
    immediately on some fzf builds, so the placeholder is required."""
    cmd = [
        "fzf", "--prompt", prompt, "--print-query", "--no-sort", "--reverse",
        "--info", "hidden", "--disabled",  # focus the query box; list is just a hint
        *_theme_args(),
        "--header", "digita e premi INVIO · ESC: indietro",
    ]  # fmt: skip
    # One hint row; with --disabled the user cannot select it, only submit the query.
    lines = "digita il titolo…\n"
    proc = util.run_cmd(cmd, input=lines)
    if proc is None:
        _missing_fzf()
        return None
    if proc.returncode != 0:  # ESC / Ctrl-C
        return None
    # --print-query: first line is the query (may be empty); second is the selection.
    query = (proc.stdout.splitlines() or [""])[0].strip()
    return query or None
