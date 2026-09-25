"""Unit tests for local mpv playback: the play() arg/signal contract and the
mpv.conf/mpv_args inspection that decides which defaults nstream injects."""

from __future__ import annotations

import pytest

from nstream import player
from nstream.config import Config


class _FakePopen:
    """Captures argv and simulates the Lua script writing the advance signal."""

    last_args: list[str] = []

    def __init__(self, args, *a, **k):
        type(self).last_args = args
        for x in args:
            if x.startswith("--script-opts-append=nstream-signal="):
                with open(x.split("=", 2)[2], "w") as f:
                    f.write("next\n")

    def wait(self):
        return 0

    def poll(self):
        return 0


@pytest.fixture
def stub_mpv(monkeypatch):
    monkeypatch.setattr(player.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        player, "_track_position", lambda path, holder, proc: holder.update(started=1.0)
    )


# --- play() arg + signal contract -----------------------------------------


def test_play_series_loads_script_and_advances(stub_mpv):
    cfg = Config(torrentio_base="tb", hwdec="", autoplay_lead=12)
    pos, dur, adv = player.play(
        cfg, "Show · S01E02", "http://u", start=42, next_label="Show · S01E03"
    )
    args = _FakePopen.last_args
    assert "--force-media-title=Show · S01E02" in args
    assert "--start=42" in args
    assert "--no-resume-playback" in args
    assert not any("write-filename" in a for a in args)
    assert any(a.startswith("--script=") and a.endswith("nstream.lua") for a in args)
    assert "--script-opts-append=nstream-lead=12" in args
    assert "--script-opts-append=nstream-resume=42" in args  # resume toast via the script
    assert adv == "next"  # play() returns the signal string


def test_play_movie_loads_script_no_card_no_advance(stub_mpv):
    # The overlay script is always loaded (single on-screen renderer), but with no
    # next_label there's no card/signal opt and nothing can ask to advance.
    cfg = Config(torrentio_base="tb", hwdec="")
    pos, dur, adv = player.play(cfg, "Movie", "http://u", next_label=None)
    args = _FakePopen.last_args
    assert any(a.startswith("--script=") and a.endswith("nstream.lua") for a in args)
    assert not any(a.startswith("--script-opts-append=nstream-info=") for a in args)
    assert not any(a.startswith("--script-opts-append=nstream-signal=") for a in args)
    assert adv == ""  # no card / no cast key → empty signal


def test_play_movie_resume_passes_script_opt(stub_mpv):
    cfg = Config(torrentio_base="tb", hwdec="")
    player.play(cfg, "Movie", "http://u", start=100)
    assert "--script-opts-append=nstream-resume=100" in _FakePopen.last_args


def test_play_no_resume_opt_when_fresh(stub_mpv):
    cfg = Config(torrentio_base="tb", hwdec="")
    player.play(cfg, "Movie", "http://u")
    assert not any(
        a.startswith("--script-opts-append=nstream-resume=") for a in _FakePopen.last_args
    )


def test_play_aid_sid_injected(stub_mpv):
    cfg = Config(torrentio_base="tb", hwdec="")
    player.play(cfg, "Movie", "http://u", audio_id=2, sub_id=3)
    assert "--aid=2" in _FakePopen.last_args and "--sid=3" in _FakePopen.last_args


def test_play_sid_no_disables_subs(stub_mpv):
    cfg = Config(torrentio_base="tb", hwdec="")
    player.play(cfg, "Movie", "http://u", sub_id="no")
    assert "--sid=no" in _FakePopen.last_args


def test_play_no_aid_sid_by_default(stub_mpv):
    cfg = Config(torrentio_base="tb", hwdec="")
    player.play(cfg, "Movie", "http://u")
    assert not any(a.startswith(("--aid", "--sid")) for a in _FakePopen.last_args)


def test_play_hwdec_injected_when_configured(stub_mpv, monkeypatch):
    # No mpv.conf hwdec; concrete nstream value → injected as-is.
    monkeypatch.setattr(player, "_mpv_conf_get", lambda opt: None)
    cfg = Config(torrentio_base="tb", hwdec="vaapi", mpv_args=[])
    player.play(cfg, "Movie", "http://u")
    assert "--hwdec=vaapi" in _FakePopen.last_args


def test_play_hwdec_not_injected_when_user_set(stub_mpv, monkeypatch):
    # A concrete method in mpv.conf is respected, nothing injected.
    monkeypatch.setattr(player, "_mpv_conf_get", lambda opt: "vaapi" if opt == "hwdec" else None)
    cfg = Config(torrentio_base="tb", hwdec="auto-safe")
    player.play(cfg, "Movie", "http://u")
    assert not any(a.startswith("--hwdec") for a in _FakePopen.last_args)


# --- hwdec auto→vaapi upgrade (_hwdec_defaults) -----------------------------


def test_hwdec_auto_upgraded_to_detected(monkeypatch):
    """mpv.conf auto-safe + VAAPI detected → nstream pins --hwdec=vaapi (CLI wins)."""
    monkeypatch.setattr(
        player, "_mpv_conf_get", lambda opt: "auto-safe" if opt == "hwdec" else None
    )
    monkeypatch.setattr(
        player.quality, "detect_caps", lambda *a, **k: player.quality.HwCaps(vaapi=True)
    )
    monkeypatch.setattr(player.quality, "preferred_hwdec", lambda caps: "vaapi")
    assert player._hwdec_defaults(Config(torrentio_base="tb", hwdec="auto-safe")) == [
        "--hwdec=vaapi"
    ]


def test_hwdec_auto_no_detection_defers_to_conf(monkeypatch):
    """auto in mpv.conf but no GPU detected → leave mpv.conf in charge."""
    monkeypatch.setattr(
        player, "_mpv_conf_get", lambda opt: "auto-safe" if opt == "hwdec" else None
    )
    monkeypatch.setattr(
        player.quality, "detect_caps", lambda *a, **k: player.quality.HwCaps(vaapi=False)
    )
    monkeypatch.setattr(player.quality, "preferred_hwdec", lambda caps: None)
    assert player._hwdec_defaults(Config(torrentio_base="tb", hwdec="auto-safe")) == []


def test_hwdec_mpv_args_override_defers(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_get", lambda opt: "auto-safe")
    cfg = Config(torrentio_base="tb", hwdec="auto-safe", mpv_args=["--hwdec=foo"])
    assert player._hwdec_defaults(cfg) == []


def test_hwdec_disabled_when_empty(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_get", lambda opt: None)
    assert player._hwdec_defaults(Config(torrentio_base="tb", hwdec="")) == []


# --- language preference (--alang/--slang) ---------------------------------


def test_lang_defaults_injected(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"], subtitle_langs=["ita", "eng"])
    flags = player._lang_defaults(cfg)
    assert "--alang=ita,eng" in flags
    assert "--slang=ita,eng" in flags
    assert "--subs-with-matching-audio=no" in flags


def test_lang_defaults_forced_audio_lang_first(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    flags = player._lang_defaults(cfg, audio_lang="eng")
    assert "--alang=eng,ita" in flags


def test_lang_defaults_not_when_user_set_in_mpv_args(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_args=["--alang=fre"])
    flags = player._lang_defaults(cfg)
    assert not any(f.startswith("--alang") for f in flags)
    assert any(f.startswith("--slang") for f in flags)  # slang still injected


def test_lang_defaults_not_when_in_mpv_conf(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: opt == "slang")
    cfg = Config(torrentio_base="tb")
    flags = player._lang_defaults(cfg)
    assert any(f.startswith("--alang") for f in flags)
    assert not any(f.startswith("--slang") for f in flags)


# --- quiet console (--msg-level) -------------------------------------------


def test_quiet_defaults_injected(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_quiet=True)
    flags = player._quiet_defaults(cfg)
    assert flags and flags[0].startswith("--msg-level=")


def test_quiet_defaults_off(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_quiet=False)
    assert player._quiet_defaults(cfg) == []


def test_quiet_defaults_not_when_user_sets_msg_level(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: opt == "msg-level")
    cfg = Config(torrentio_base="tb", mpv_quiet=True)
    assert player._quiet_defaults(cfg) == []


# --- file-tags display (--display-tags) ------------------------------------


def test_display_tags_suppressed_by_default(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_quiet=True)
    assert player._display_tags_defaults(cfg) == ["--display-tags="]


def test_display_tags_off_when_not_quiet(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_quiet=False)
    assert player._display_tags_defaults(cfg) == []


def test_display_tags_not_when_user_sets_it(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_quiet=True, mpv_args=["--display-tags=Title"])
    assert player._display_tags_defaults(cfg) == []


# --- anti-desync stream cache (_stream_cache_defaults) ----------------------


def test_stream_cache_defaults_injected(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    assert player._stream_cache_defaults(Config(torrentio_base="tb")) == [
        "--demuxer-max-bytes=512MiB",
        "--cache-pause-initial=yes",
        "--cache-pause-wait=3",
    ]


def test_stream_cache_defaults_not_when_user_sets_demuxer_max_bytes(monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", mpv_args=["--demuxer-max-bytes=1GiB"])
    flags = player._stream_cache_defaults(cfg)
    assert not any(f.startswith("--demuxer-max-bytes") for f in flags)
    assert "--cache-pause-initial=yes" in flags


def test_stream_cache_defaults_defer_when_user_manages_cache_pause(monkeypatch):
    # cache-pause in mpv.conf → the whole pause family is the user's.
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: opt == "cache-pause")
    flags = player._stream_cache_defaults(Config(torrentio_base="tb"))
    assert flags == ["--demuxer-max-bytes=512MiB"]


def test_stream_cache_defaults_partial_when_user_sets_wait_in_conf(monkeypatch):
    # mpv.conf keys are exact: managing only the wait keeps the initial-pause default.
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: opt == "cache-pause-wait")
    flags = player._stream_cache_defaults(Config(torrentio_base="tb"))
    assert "--cache-pause-initial=yes" in flags
    assert not any(f.startswith("--cache-pause-wait") for f in flags)


def test_play_stream_cache_injected(stub_mpv, monkeypatch):
    monkeypatch.setattr(player, "_mpv_conf_has", lambda opt: False)
    cfg = Config(torrentio_base="tb", hwdec="")
    player.play(cfg, "Movie", "http://u")
    args = _FakePopen.last_args
    assert "--demuxer-max-bytes=512MiB" in args
    assert "--cache-pause-initial=yes" in args
    assert "--cache-pause-wait=3" in args


# --- lua ↔ player signal-file contract (source-level tripwire) ---------------


def test_lua_signal_contract_pinned():
    """play() and the mpv overlay share a tiny file protocol but neither imports the
    other, and no harness executes the Lua — so pin the contract at the source level:
    the script must read its options under the "nstream" prefix (play() passes
    `--script-opts-append=nstream-signal=<path>`) and write exactly the literals
    play() strips and returns ("next" / "cast"). Renaming either side breaks this
    test before it breaks a movie night."""
    import pathlib

    lua = (pathlib.Path(player.__file__).parent / "nstream.lua").read_text()
    assert 'options.read_options(opts, "nstream")' in lua
    assert "signal" in lua  # the opts key play() appends as nstream-signal=
    assert 'f:write("next\\n")' in lua
    assert 'f:write("cast\\n")' in lua
    # play() strips the trailing newline: both spellings must land on the same tokens
    assert "next\n".strip() == "next" and "cast\n".strip() == "cast"
