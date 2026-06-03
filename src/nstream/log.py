"""Logging for nstream: a rotating file log plus an optional debug console.

stdlib-only. The single hard rule is that **secrets never reach the log**: Torrentio
URLs embed the debrid token, so a `RedactFilter` scrubs every record (provider
`key=token` segments and `/resolve/<provider>/<token>/` paths) regardless of what the
caller passed. The file log exists so an unexpected crash — easy to lose when nstream
runs inside the foot launcher — is captured for diagnosis.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

# Debrid token carriers to redact. Provider `key=token` (Torrentio config / URLs) and
# the resolve path `/resolve/<provider>/<token>/`.
_PROVIDERS = "realdebrid|alldebrid|premiumize|torbox|debridlink|easydebrid|offcloud|putio"
_REDACTIONS = (
    (re.compile(rf"\b({_PROVIDERS})=[^|&\s\"']+", re.I), r"\1=<redacted>"),
    (re.compile(r"(/resolve/[^/]+/)[^/?\s]+", re.I), r"\1<redacted>"),
)


def redact(text: str) -> str:
    for pat, repl in _REDACTIONS:
        text = pat.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    """Scrub secrets from a record's final message (after %-formatting)."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        scrubbed = redact(msg)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


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
    redactor = RedactFilter()

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        fh.addFilter(redactor)
        logger.addHandler(fh)
    except OSError:
        pass  # file logging is best-effort; never block playback on it

    if debug:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter("[%(name)s] %(levelname)s %(message)s"))
        sh.addFilter(redactor)
        logger.addHandler(sh)

    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"nstream.{name}")
