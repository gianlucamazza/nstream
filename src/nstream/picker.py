"""fzf-backed list pickers shared by the TUI flow and the cast device/audio menus.

Kept in its own module (importing nothing else from nstream) so both cli and caster can
use it without an import cycle. `_run_fzf` is the core; `fzf` returns just the value,
`fzf_key` also reports which key (Enter/Tab/Alt-C) made the selection.
"""

from __future__ import annotations

import sys

from . import util


def _run_fzf[T](
    items: list[tuple[str, T]],
    prompt: str,
    *,
    header: str | None = None,
    expect: tuple[str, ...] = (),
) -> tuple[str, T] | None:
    """Core fzf picker. Returns (key, value): `key` is "" for Enter or one of
    `expect` (e.g. "tab") when that key was pressed; None on ESC/no match.

    With `expect`, fzf prints the pressed key as the first stdout line (empty for
    Enter) before the selection, so the single-item shortcut is skipped to keep the
    alternate key reachable."""
    if not items:
        return None
    if len(items) == 1 and header is None and not expect:
        return ("", items[0][1])
    # Hidden leading index lets labels repeat without ambiguity.
    lines = "".join(f"{i}\t{label}\n" for i, (label, _) in enumerate(items))
    # No --height → fzf takes the full alternate screen and restores the terminal on
    # exit, so menus never pile up in the scrollback (clean TUI). --cycle wraps nav.
    cmd = ["fzf", "--prompt", prompt, "--with-nth", "2..",
           "--delimiter", "\t", "--no-sort", "--reverse", "--cycle"]  # fmt: skip
    if header:
        cmd += ["--header", header]
    if expect:
        cmd += ["--expect", ",".join(expect)]
    proc = util.run_cmd(cmd, input=lines)
    if proc is None:
        print("nstream: fzf non trovato", file=sys.stderr)
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


def fzf[T](items: list[tuple[str, T]], prompt: str, *, header: str | None = None) -> T | None:
    """Pick one of (label, value) pairs via fzf. Returns the value or None.

    `header` shows a transient notice above the list (e.g. why a title couldn't
    play) — it survives the menu reopening, unlike a stderr line that scrolls away.
    A single item is returned directly only when there's nothing to announce."""
    chosen = _run_fzf(items, prompt, header=header)
    return chosen[1] if chosen else None


def fzf_key[T](
    items: list[tuple[str, T]],
    prompt: str,
    *,
    header: str | None = None,
    expect: tuple[str, ...] = ("tab", "alt-c"),
) -> tuple[str, T] | None:
    """Like `fzf` but also reports which key selected the item — used by the leaf
    title/continue lists where Tab flips auto ↔ manual playback for that pick."""
    return _run_fzf(items, prompt, header=header, expect=expect)
