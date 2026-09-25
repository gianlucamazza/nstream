"""Cross-frontend local playback policy, with explicit delivery evidence."""

import pytest

from nstream import application, subs
from nstream.config import Config, PlayOpts
from nstream.playback import PlaybackError, PlaybackOutcome


def _request(tmp_path, *, auto=True):
    opts = PlayOpts(
        auto=auto,
        cast=False,
        sub_mode=None,
        sub_lang=None,
        history=False,
        autoplay=False,
        audio_lang="ita",
    )
    return application.LocalRequest(
        "Fixture",
        "movie",
        "tt1",
        {"url": "https://example.test/video"},
        opts,
        str(tmp_path),
        auto=auto,
        safety_sub_lang="ita",
    )


def test_local_policy_threads_audio_and_subtitle_evidence(tmp_path):
    seen = {}
    pick = subs.SubsPick(("fixture.srt",), "hash")

    def acquire(*args, **kwargs):
        seen["sub_lang"] = kwargs["safety_sub_lang"]
        return pick

    def play(*args, **kwargs):
        seen.update(kwargs)
        return PlaybackOutcome(started=True)

    result = application.play_local(
        Config(), _request(tmp_path), backend=play, acquire_subs=acquire
    )
    assert result is not None and result.subtitles == pick
    assert seen["audio_lang"] == seen["sub_lang"] == "ita"
    assert seen["sub_paths"] == ("fixture.srt",)


def test_track_cancel_has_no_backend_effect(tmp_path):
    result = application.play_local(
        Config(),
        _request(tmp_path, auto=False),
        choose_tracks=lambda *args: None,
        backend=lambda *args, **kwargs: pytest.fail("cancelled playback launched a backend"),
    )
    assert result is None


def test_unobserved_backend_cannot_produce_success(tmp_path):
    with pytest.raises(PlaybackError):
        application.play_local(
            Config(),
            _request(tmp_path),
            acquire_subs=lambda *args, **kwargs: subs.SubsPick(),
            backend=lambda *args, **kwargs: PlaybackOutcome(),
        )
