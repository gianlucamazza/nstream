"""Playback success requires media evidence, never a bare successful process exit."""

import pytest

from nstream import player
from nstream.config import Config
from nstream.playback import PlaybackError, PlaybackOutcome, require_started


def test_unobserved_zero_exit_is_not_success():
    with pytest.raises(PlaybackError):
        require_started(PlaybackOutcome())
    assert require_started(PlaybackOutcome(started=True)).started


def test_missing_player_is_a_domain_error(monkeypatch):
    monkeypatch.setattr(player, "_hwdec_defaults", lambda cfg: [])

    def missing(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(player.subprocess, "Popen", missing)
    with pytest.raises(PlaybackError) as exc:
        player.play(Config(), "Test", "http://example.test/SECRET")
    assert exc.value.code == "player_missing" and "SECRET" not in str(exc.value)


def test_interrupt_reaps_owned_player(monkeypatch):
    calls = []

    class Process:
        def poll(self):
            return None

        def wait(self, timeout=None):
            if timeout is None:
                raise KeyboardInterrupt()
            calls.append("reaped")

        def terminate(self):
            calls.append("terminate")

    monkeypatch.setattr(player, "_hwdec_defaults", lambda cfg: [])
    monkeypatch.setattr(player, "_track_position", lambda *args: None)
    monkeypatch.setattr(player.subprocess, "Popen", lambda *args, **kwargs: Process())
    with pytest.raises(KeyboardInterrupt):
        player.play(Config(), "Test", "http://example.test/SECRET")
    assert calls == ["terminate", "reaped"]
