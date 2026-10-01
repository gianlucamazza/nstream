"""Logging for nstream: a rotating file log plus an optional debug console.

stdlib-only. The single hard rule is that **secrets never reach the log**: Torrentio
URLs embed the debrid token, so a `RedactFormatter` scrubs every fully formatted record
— message *and* exception traceback (provider `key=token` segments and
`/resolve/<provider>/<token>/` paths) — regardless of what the caller passed. The file
log exists so an unexpected crash — easy to lose when nstream runs inside the foot
launcher — is captured for diagnosis.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import logging.handlers
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .config import DEBRID_PROVIDERS

# Debrid token carriers to redact. Provider `key=token` (Torrentio config / URLs), the
# resolve path `/resolve/<provider>/<token>/`, a `token=` query param (TorBox requestdl)
# and an `Authorization: Bearer <token>` header (native debrid API calls). The provider
# alternation is derived from config's single source so it can never drift.
_PROVIDERS = "|".join(DEBRID_PROVIDERS)
_REDACTIONS = (
    (re.compile(rf"\b({_PROVIDERS})=[^|&\s\"']+", re.I), r"\1=<redacted>"),
    (re.compile(r"(/resolve/[^/]+/)[^/?\s]+", re.I), r"\1<redacted>"),
    (re.compile(r"([?&]token=)[^|&\s\"']+", re.I), r"\1<redacted>"),
    (re.compile(r"(Bearer\s+)\S+", re.I), r"\1<redacted>"),
)


def redact(text: str) -> str:
    for pat, repl in _REDACTIONS:
        text = pat.sub(repl, text)
    # Arbitrary addon capability URLs cannot be covered by provider-specific patterns.
    return re.sub(r"https?://[^\s\"']+", "<url>", text, flags=re.I)


def public_value(value):
    """Scrub machine-readable output too, including arbitrary backend event fields."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: public_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [public_value(item) for item in value]
    return value


def emit_json(obj: dict) -> None:
    """The one writer of machine-readable output (ADR 0037): exactly one scrubbed JSON object
    per line on stdout. Every `--json` path goes through here, so no emitter can forget the
    url/token scrub."""
    import json

    from . import notices

    # Domain notices collected during this --json run (ADR 0037) ride along on the result
    # object; per-event JSONL lines (`--follow`) don't repeat them.
    bag = notices.collected()
    if bag is not None and "notices" not in obj and "event" not in obj:
        obj = {**obj, "notices": bag}
    sys.stdout.write(json.dumps(public_value(obj), ensure_ascii=False) + "\n")
    sys.stdout.flush()


class RedactFilter(logging.Filter):
    """Scrub secrets from a record's final message (after %-formatting).

    Note: a filter only sees the message — exception tracebacks are appended by the
    Formatter *after* filtering, so handlers must use `RedactFormatter` to cover them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        scrubbed = redact(msg)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


class RedactFormatter(logging.Formatter):
    """Scrub secrets from the complete formatted output, traceback included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that keeps the log 0600 — on the initial open, after every
    rotation, and tightening a pre-existing world-readable file. The log is secret-free
    by design, but the redaction is a regex blocklist: defense in depth at zero cost,
    and consistent with config.json/history.json (both 0600)."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)  # O_CREAT mode only applies to new files
        return os.fdopen(fd, self.mode, encoding=self.encoding)


def log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "nstream" / "nstream.log"


_configured = False


def setup_logging(debug: bool = False) -> logging.Logger:
    """Configure the `nstream` logger: a rotating file handler always, plus a stderr
    handler when `debug`. Idempotent — re-calling only adjusts the level."""
    global _configured
    logger = logging.getLogger("nstream")
    level = logging.DEBUG if debug else logging.INFO
    logger.setLevel(level)
    if _configured:
        return logger
    logger.propagate = False

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = _PrivateRotatingFileHandler(
            str(path), maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(RedactFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(fh)
    except OSError:
        pass  # file logging is best-effort; never block playback on it

    if debug:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(RedactFormatter("[%(name)s] %(levelname)s %(message)s"))
        logger.addHandler(sh)

    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"nstream.{name}")


def phase(name: str):
    """Measure a named phase without recording arguments, results, or content identifiers."""

    def decorate[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def measured(*args: P.args, **kwargs: P.kwargs) -> R:
            started = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                get_logger("timing").debug(
                    "phase=%s elapsed_ms=%.1f", name, (time.monotonic() - started) * 1000
                )

        return measured

    return decorate
