"""Unit tests for hardware-aware stream parsing, caps detection and ranking."""

from __future__ import annotations

from nstream import quality
from nstream.config import Stream
from nstream.quality import Caps

# Real Torrentio samples (name, title) captured from "Superman" (2025).
S_8K: Stream = {
    "name": "[RD+] Torrentio\n4320p HDR",
    "title": "Superman (2025) 4320p HDR Ai Upscale 7.1 -Mesc\n👤 6 💾 41.27 GB ⚙️ ThePirateBay",
}
S_REMUX_DVP7: Stream = {
    "name": "[RD+] Torrentio\n4k DV | HDR",
    "title": "Superman.2025.2160p.UHD.BluRay.REMUX.DV.P7.HDR-BTM\n👤 30 💾 65.71 GB ⚙️ ThePirateBay",
}
S_WEBDL_4K: Stream = {
    "name": "[RD+] Torrentio\n4k HDR",
    "title": "Superman.2025.iTA-ENG.WEBDL.2160p.HEVC.HDR.x265-CYBER.mkv\n👤 5 💾 24.09 GB ⚙️ 1337x",
}
S_1080: Stream = {
    "name": "Torrentio\n1080p",
    "title": "Superman.2025.1080p.BluRay.x264-GROUP\n👤 100 💾 8.50 GB ⚙️ 1337x",
}
S_AV1: Stream = {
    "name": "[RD+] Torrentio\n2160p",
    "title": "Superman.2025.2160p.WEB-DL.AV1.HDR-AOM\n👤 3 💾 12.00 GB ⚙️ x",
}
S_DVP5: Stream = {
    "name": "[RD+] Torrentio\n4k DV",
    "title": "Superman.2025.2160p.DV.P5.HEVC-XX\n👤 1 💾 28.30 GB ⚙️ x",
}


def test_parse_8k_upscale():
    i = quality.parse_stream(S_8K)
    assert i.resolution == 4320
    assert i.hdr and i.cached
    assert i.size_gb == 41.27 and i.seeders == 6


def test_parse_remux_dv_p7():
    i = quality.parse_stream(S_REMUX_DVP7)
    assert i.resolution == 2160 and i.codec == ""  # this REMUX title has no codec token
    assert i.dv and i.dv_profile == 7 and i.size_gb == 65.71


def test_parse_webdl_4k_hevc():
    i = quality.parse_stream(S_WEBDL_4K)
    assert (i.resolution, i.codec) == (2160, "hevc") and i.hdr and not i.dv


def test_parse_1080_h264():
    i = quality.parse_stream(S_1080)
    assert (i.resolution, i.codec) == (1080, "h264")
    assert not i.cached and i.seeders == 100


def test_parse_av1_and_dvp5():
    assert quality.parse_stream(S_AV1).codec == "av1"
    assert quality.parse_stream(S_DVP5).dv_profile == 5


# --- language / source / release_name parsing ------------------------------


def test_parse_languages_and_source():
    i = quality.parse_stream(S_WEBDL_4K)  # ...iTA-ENG.WEBDL...
    assert i.languages == frozenset({"ita", "eng"})
    assert i.source == "webdl"
    assert i.release_name.startswith("Superman.2025.iTA-ENG")


def test_parse_source_remux_beats_bluray():
    assert quality.parse_stream(S_REMUX_DVP7).source == "remux"


def test_parse_untagged_language_empty():
    s: Stream = {"name": "Torrentio\n1080p", "title": "Movie.2025.1080p.BluRay.x264-GROUP\n👤 9"}
    i = quality.parse_stream(s)
    assert i.languages == frozenset() and i.source == "bluray"


def test_parse_multi_and_camrip():
    s: Stream = {"name": "Torrentio\n720p", "title": "Film.2025.MULTI.720p.HDCAM-XX\n👤 2 💾 1 GB"}
    i = quality.parse_stream(s)
    assert "multi" in i.languages and i.source == "cam"


def test_parse_no_false_positive_group():
    s: Stream = {"name": "Torrentio\n1080p", "title": "Movie.2025.1080p.WEB-DL-CYBERENG\n👤 5"}
    # "CYBERENG" must not match ENG (no word boundary).
    assert "eng" not in quality.parse_stream(s).languages


# --- new exclusion reasons -------------------------------------------------

CAPS = Caps(codecs=frozenset({"h264", "hevc", "hevc10", "vp9", "av1"}), max_resolution=2160)


def test_reason_camrip():
    info = quality.parse_stream(
        {"name": "Torrentio\n720p", "title": "F.2025.720p.CAM-X\n👤 5 💾 1 GB"}
    )
    assert quality.unsupported_reason(info, CAPS, 2160, exclude_camrip=True) == "camrip (cam)"


def test_reason_language():
    info = quality.parse_stream(
        {"name": "Torrentio\n1080p", "title": "F.2025.FRENCH.1080p.WEB-DL\n👤 9 💾 2 GB"}
    )
    r = quality.unsupported_reason(info, CAPS, 2160, audio_langs=("ita", "eng"), lang_filter=True)
    assert r == "lingua fra"


def test_reason_language_keeps_untagged_and_multi():
    unt = quality.parse_stream({"name": "Torrentio\n1080p", "title": "F.2025.1080p.WEB-DL\n👤 9"})
    mul = quality.parse_stream({"name": "Torrentio\n1080p", "title": "F.MULTI.1080p.WEB-DL\n👤 9"})
    for info in (unt, mul):
        assert (
            quality.unsupported_reason(
                info, CAPS, 2160, audio_langs=("ita", "eng"), lang_filter=True
            )
            is None
        )


def test_reason_low_seeders_only_non_cached():
    dead = quality.parse_stream(
        {"name": "Torrentio\n1080p", "title": "F.1080p.WEB-DL\n👤 1 💾 2 GB"}
    )
    assert quality.unsupported_reason(dead, CAPS, 2160, min_seeders=3) == "pochi seeder"
    cached = quality.parse_stream({"name": "[RD+] Torrentio\n1080p", "title": "F.1080p\n👤 1"})
    assert quality.unsupported_reason(cached, CAPS, 2160, min_seeders=3) is None


def test_rank_dedup_keeps_best():
    rel = "Superman.2025.1080p.BluRay.x264-GROUP"
    s_low: Stream = {"name": "Torrentio\n1080p", "title": f"{rel}\n👤 5 💾 8 GB ⚙️ a"}
    s_high: Stream = {"name": "[RD+] Torrentio\n1080p", "title": f"{rel}\n👤 50 💾 8 GB ⚙️ b"}
    playable, _ = quality.rank_streams(
        [s_low, s_high],
        CAPS,
        max_resolution=2160,
        allow_software=False,
        allow_dv5=False,
        dedup=True,
    )
    assert len(playable) == 1 and playable[0].info.cached  # kept the [RD+] copy


def test_rank_lang_filter_moves_to_excluded():
    fr: Stream = {"name": "Torrentio\n1080p", "title": "F.2025.FRENCH.1080p.WEB-DL\n👤 9 💾 2 GB"}
    en: Stream = {"name": "Torrentio\n1080p", "title": "F.2025.1080p.WEB-DL\n👤 9 💾 2 GB"}
    playable, excluded = quality.rank_streams(
        [fr, en], CAPS, max_resolution=2160, allow_software=False, allow_dv5=False,
        audio_langs=("ita", "eng"), lang_filter=True,
    )  # fmt: skip
    assert len(playable) == 1 and len(excluded) == 1
    assert excluded[0].reason == "lingua fra"


# --- caps from vainfo ------------------------------------------------------

VAINFO_NO_AV1 = """\
      VAProfileH264Main               : VAEntrypointVLD
      VAProfileHEVCMain               : VAEntrypointVLD
      VAProfileHEVCMain10             : VAEntrypointVLD
      VAProfileVP9Profile2            : VAEntrypointVLD
      VAProfileH264Main               : VAEntrypointEncSlice
"""
VAINFO_WITH_AV1 = VAINFO_NO_AV1 + "      VAProfileAV1Profile0            : VAEntrypointVLD\n"


def test_caps_from_vainfo_no_av1():
    c = quality._caps_from_vainfo(VAINFO_NO_AV1)
    assert "hevc10" in c and "hevc" in c and "h264" in c and "vp9" in c
    assert "av1" not in c


def test_caps_from_vainfo_with_av1():
    assert "av1" in quality._caps_from_vainfo(VAINFO_WITH_AV1)


def test_caps_ignores_encode_only():
    # Encode entrypoints must not count as decode support.
    assert (
        quality._caps_from_vainfo("      VAProfileAV1Profile0 : VAEntrypointEncSlice\n")
        == frozenset()
    )


def test_detect_caps_fallback_when_vainfo_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(quality.subprocess, "run", boom)
    caps = quality.detect_caps(use_cache=False)
    assert "hevc" in caps.codecs and "av1" not in caps.codecs  # conservative default
    assert caps.vaapi is False  # no real probe → don't claim VAAPI


def test_detect_caps_sets_vaapi_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    class _P:
        stdout = VAINFO_NO_AV1
        stderr = ""

    monkeypatch.setattr(quality.subprocess, "run", lambda *a, **k: _P())
    caps = quality.detect_caps(use_cache=False)
    assert caps.vaapi is True
    # Cached round-trip preserves the vaapi flag (cache v2).
    cached = quality.detect_caps(use_cache=True)
    assert cached.vaapi is True and "hevc" in cached.codecs


def test_preferred_hwdec():
    assert quality.preferred_hwdec(Caps(vaapi=True)) == "vaapi"
    assert quality.preferred_hwdec(Caps(vaapi=False)) is None


# --- ranking ---------------------------------------------------------------

CAPS_NO_AV1 = Caps(codecs=frozenset({"h264", "hevc", "hevc10", "vp9"}), max_resolution=2160)


def _rank(streams, **kw):
    opts = {"max_resolution": 2160, "allow_software": False, "allow_dv5": False}
    opts.update(kw)
    return quality.rank_streams(streams, CAPS_NO_AV1, **opts)


def test_rank_excludes_8k_av1_dvp5():
    streams = [S_8K, S_AV1, S_DVP5, S_WEBDL_4K, S_1080]
    playable, excluded = _rank(streams)
    reasons = {r.reason for r in excluded}
    assert any(x == "8K" for x in reasons)
    assert any("AV1" in x for x in reasons)
    assert any("P5" in x for x in reasons)
    # the 4K HEVC and 1080p remain playable
    pl_titles = [r.stream["title"] for r in playable]
    assert any("WEBDL.2160p" in t for t in pl_titles)
    assert any("1080p" in t for t in pl_titles)


def test_rank_orders_cached_and_resolution_first():
    playable, _ = _rank([S_1080, S_WEBDL_4K])
    # cached 4K should rank above non-cached 1080p
    assert playable[0].stream is S_WEBDL_4K


def test_allow_software_keeps_av1():
    playable, excluded = _rank([S_AV1], allow_software=True)
    assert len(playable) == 1 and not excluded


def test_allow_dv5_keeps_dvp5():
    playable, excluded = _rank([S_DVP5], allow_dv5=True)
    assert len(playable) == 1 and not excluded


def test_unlimited_resolution_keeps_8k():
    playable, excluded = _rank([S_8K], max_resolution=0)
    assert len(playable) == 1 and not excluded
