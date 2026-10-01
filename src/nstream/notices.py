"""User-facing notices from the domain, visible to `--json` too (ADR 0037).

Domain modules used to `print("nstream: …", file=sys.stderr)` facts an agent needs — the P2P
gate refusing, a dub fallback, subtitles fetched but not attached — and `--json` lost them
all. `emit` keeps the stderr line byte-identical and, while a `capture()` is active, also
collects the notice; headless adds the collected list to its JSON object as `notices`.

Bottom tier (stdlib only): any module may import it. The sink is module-level with a lock,
not a ContextVar, because availability probes run on a thread pool that would not inherit it.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Notice:
    text: str
    code: str = ""  # stable id for agents ("" = informational, no contract)
    level: str = "warn"  # "info" | "warn" | "fail"


_lock = threading.Lock()
_bags: list[list[Notice]] = []


def emit(text: str, *, code: str = "", level: str = "warn", render: str | None = None) -> None:
    """Print `render` (default `nstream: <text>`) on stderr, and collect the notice for every
    active `capture()`."""
    print(render if render is not None else f"nstream: {text}", file=sys.stderr)
    with _lock:
        for bag in _bags:
            bag.append(Notice(text, code, level))


@contextmanager
def capture() -> Iterator[list[Notice]]:
    """Collect the notices emitted inside the block (printing is unchanged)."""
    bag: list[Notice] = []
    with _lock:
        _bags.append(bag)
    try:
        yield bag
    finally:
        with _lock:
            _bags.remove(bag)


def collected() -> list[dict] | None:
    """The innermost active capture as JSON-ready dicts, or None when nothing captures."""
    with _lock:
        if not _bags:
            return None
        return [asdict(n) for n in _bags[-1]]
