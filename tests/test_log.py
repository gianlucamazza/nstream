"""Unit tests for logging setup and the secret-redaction filter."""

from __future__ import annotations

import logging

from nstream import log


def test_redact_provider_tokens():
    s = "sort=qualitysize|realdebrid=ABC123SECRET"
    assert log.redact(s) == "sort=qualitysize|realdebrid=<redacted>"
    assert "ABC123SECRET" not in log.redact(s)


def test_redact_all_providers():
    for key in ("alldebrid", "premiumize", "torbox", "putio", "easydebrid"):
        out = log.redact(f"{key}=TOKENXYZ other")
        assert out == f"{key}=<redacted> other"


def test_redact_resolve_url():
    url = "https://torrentio.strem.fun/resolve/realdebrid/TOKEN123/abc/movie.mkv"
    out = log.redact(url)
    assert "TOKEN123" not in out
    assert "/resolve/realdebrid/<redacted>" in out


def test_redact_noop_when_clean():
    assert log.redact("just a normal message") == "just a normal message"


def test_redact_filter_scrubs_record():
    rec = logging.LogRecord(
        "nstream.x", logging.INFO, __file__, 1, "catt cast realdebrid=SECRET", None, None
    )
    assert log.RedactFilter().filter(rec) is True
    assert "SECRET" not in rec.getMessage()


def test_setup_logging_writes_redacted_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # Force a fresh configuration for this test's handlers.
    monkeypatch.setattr(log, "_configured", False)
    logger = logging.getLogger("nstream")
    monkeypatch.setattr(logger, "handlers", [])
    log.setup_logging(debug=False)
    logger.info("playback url=%s", "https://x/resolve/realdebrid/TоK/y")
    for h in logger.handlers:
        h.flush()
    text = (tmp_path / "nstream" / "nstream.log").read_text()
    assert "playback" in text
    assert "/resolve/realdebrid/<redacted>" in text


def test_log_path_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert log.log_path() == tmp_path / "nstream" / "nstream.log"
