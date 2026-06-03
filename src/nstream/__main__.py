"""Enable `python -m nstream` (used as the preview-command fallback when the installed
console script isn't on PATH, e.g. editable/dev checkouts and tests)."""

from __future__ import annotations

from .cli import _entry

_entry()
