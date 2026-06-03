"""Unit tests for hardware-aware stream parsing, caps detection and ranking."""

from __future__ import annotations

from nstream import quality
from nstream.config import Stream
from nstream.quality import Caps, FilterSpec

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
    spec = FilterSpec(max_resolution=2160, exclude_camrip=True)
    assert quality.unsupported_reason(info, CAPS, spec) == "camrip (cam)"


def test_reason_language():
    info = quality.parse_stream(
        {"name": "Torrentio\n1080p", "title": "F.2025.FRENCH.1080p.WEB-DL\n👤 9 💾 2 GB"}
    )
    spec = FilterSpec(max_resolution=2160, audio_langs=("ita", "eng"), lang_filter=True)
    assert quality.unsupported_reason(info, CAPS, spec) == "lingua fra"


def test_reason_language_keeps_untagged_and_multi():
    unt = quality.parse_stream({"name": "Torrentio\n1080p", "title": "F.2025.1080p.WEB-DL\n👤 9"})
    mul = quality.parse_stream({"name": "Torrentio\n1080p", "title": "F.MULTI.1080p.WEB-DL\n👤 9"})
    spec = FilterSpec(max_resolution=2160, audio_langs=("ita", "eng"), lang_filter=True)
    for info in (unt, mul):
        assert quality.unsupported_reason(info, CAPS, spec) is None


def test_reason_low_seeders_only_non_cached():
    dead = quality.parse_stream(
        {"name": "Torrentio\n1080p", "title": "F.1080p.WEB-DL\n👤 1 💾 2 GB"}
    )
    spec = FilterSpec(max_resolution=2160, min_seeders=3)
    assert quality.unsupported_reason(dead, CAPS, spec) == "pochi seeder"
    cached = quality.parse_stream({"name": "[RD+] Torrentio\n1080p", "title": "F.1080p\n👤 1"})
    assert quality.unsupported_reason(cached, CAPS, spec) is None


def test_rank_dedup_keeps_best():
    rel = "Superman.2025.1080p.BluRay.x264-GROUP"
    s_low: Stream = {"name": "Torrentio\n1080p", "title": f"{rel}\n👤 5 💾 8 GB ⚙️ a"}
    s_high: Stream = {"name": "[RD+] Torrentio\n1080p", "title": f"{rel}\n👤 50 💾 8 GB ⚙️ b"}
    spec = FilterSpec(max_resolution=2160, dedup=True)
    playable, _ = quality.rank_streams([s_low, s_high], CAPS, spec)
    assert len(playable) == 1 and playable[0].info.cached  # kept the [RD+] copy


def test_rank_lang_filter_moves_to_excluded():
    fr: Stream = {"name": "Torrentio\n1080p", "title": "F.2025.FRENCH.1080p.WEB-DL\n👤 9 💾 2 GB"}
    en: Stream = {"name": "Torrentio\n1080p", "title": "F.2025.1080p.WEB-DL\n👤 9 💾 2 GB"}
    spec = FilterSpec(max_resolution=2160, audio_langs=("ita", "eng"), lang_filter=True)
    playable, excluded = quality.rank_streams([fr, en], CAPS, spec)
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
    # run_cmd returns None when vainfo is missing or fails.
    monkeypatch.setattr(quality.util, "run_cmd", lambda *a, **k: None)
    caps = quality.detect_caps(use_cache=False)
    assert "hevc" in caps.codecs and "av1" not in caps.codecs  # conservative default
    assert caps.vaapi is False  # no real probe → don't claim VAAPI


def test_detect_caps_sets_vaapi_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    class _P:
        stdout = VAINFO_NO_AV1
        stderr = ""

    monkeypatch.setattr(quality.util, "run_cmd", lambda *a, **k: _P())
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


def _rank(streams, *, max_resolution=2160, allow_software=False, allow_dv5=False):
    spec = FilterSpec(
        max_resolution=max_resolution, allow_software=allow_software, allow_dv5=allow_dv5
    )
    return quality.rank_streams(streams, CAPS_NO_AV1, spec)


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


# --- audio codec + cast compatibility --------------------------------------

S_REMUX_TRUEHD: Stream = {
    "name": "[RD+] Torrentio\n4k",
    "title": "Movie.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-GRP\n👤 20 💾 60.0 GB ⚙️ x",
}
S_WEBDL_EAC3: Stream = {
    "name": "[RD+] Torrentio\n1080p",
    "title": "Movie.2160p.WEB-DL.HEVC.DDP5.1-GRP\n👤 10 💾 9.0 GB ⚙️ x",
}
S_AC3: Stream = {
    "name": "[RD+] Torrentio\n1080p",
    "title": "Movie.1080p.BluRay.x264.AC3-GRP\n👤 50 💾 8.0 GB ⚙️ x",
}
S_DTSHD: Stream = {
    "name": "[RD+] Torrentio\n1080p",
    "title": "Movie.1080p.BluRay.x264.DTS-HD.MA.5.1-GRP\n👤 5 💾 12.0 GB ⚙️ x",
}


S_BDREMUX_UNTAGGED: Stream = {
    "name": "[RD+] Torrentio\n4k",
    "title": "The.Matrix.1999.4K.HDR.2160p.BDRemux.Ita.Eng.x265-NAHOM\n👤 15 💾 40.0 GB ⚙️ x",
}


def test_parse_bdremux_is_remux():
    # "BDRemux" (no separator) must still be recognised as a remux source.
    assert quality.parse_stream(S_BDREMUX_UNTAGGED).source == "remux"


def test_reason_cast_audio_demotes_untagged_remux():
    info = quality.parse_stream(S_BDREMUX_UNTAGGED)
    assert info.audio == ""  # no audio token in the title
    cast_spec = FilterSpec(max_resolution=2160, cast_audio=True)
    assert quality.unsupported_reason(info, quality.cast_caps(), cast_spec) == "audio remux"
    # but a non-cast rank keeps it (mpv decodes lossless locally)
    assert (
        quality.unsupported_reason(info, quality.cast_caps(), FilterSpec(max_resolution=2160))
        is None
    )


def test_parse_audio_codecs():
    assert quality.parse_stream(S_REMUX_TRUEHD).audio == "truehd"  # TrueHD beats AC3 token
    assert quality.parse_stream(S_WEBDL_EAC3).audio == "eac3"
    assert quality.parse_stream(S_AC3).audio == "ac3"
    assert quality.parse_stream(S_DTSHD).audio == "dtshd"
    assert quality.parse_stream({"title": "Movie.1080p.AAC-X"}).audio == "aac"
    assert quality.parse_stream(S_WEBDL_4K).audio == ""  # untagged audio


def test_cast_caps_profile():
    caps = quality.cast_caps()
    assert "av1" not in caps.codecs
    assert {"h264", "hevc", "hevc10", "vp9"} <= caps.codecs
    assert caps.max_resolution == 2160


def test_reason_cast_audio_excludes_lossless():
    cast_spec = FilterSpec(max_resolution=2160, cast_audio=True)
    for s in (S_REMUX_TRUEHD, S_DTSHD):
        info = quality.parse_stream(s)
        r = quality.unsupported_reason(info, quality.cast_caps(), cast_spec)
        assert r and r.startswith("audio ")
    # compatible / unknown audio passes
    for s in (S_WEBDL_EAC3, S_AC3, S_WEBDL_4K):
        info = quality.parse_stream(s)
        assert quality.unsupported_reason(info, quality.cast_caps(), cast_spec) is None


def test_rank_cast_audio_demotes_lossless():
    spec = FilterSpec(max_resolution=2160, cast_audio=True)
    playable, excluded = quality.rank_streams(
        [S_REMUX_TRUEHD, S_WEBDL_EAC3], quality.cast_caps(), spec
    )
    assert [r.stream for r in playable] == [S_WEBDL_EAC3]
    assert len(excluded) == 1 and excluded[0].stream is S_REMUX_TRUEHD


def test_rank_no_cast_audio_keeps_lossless():
    # Without cast_audio the TrueHD remux stays playable (local mpv decodes it).
    playable, _ = _rank([S_REMUX_TRUEHD])
    assert len(playable) == 1


def test_cached_marker_provider_agnostic():
    # Torrentio marks instant streams per-debrid: [RD+]/[AD+]/[PM+]/[TB+]/[Putio+].
    for mark in ("[RD+]", "[AD+]", "[PM+]", "[TB+]", "[Putio+]"):
        assert quality.parse_stream({"name": f"{mark} Torrentio\n1080p"}).cached is True
    # Non-cached / no marker → not cached.
    assert quality.parse_stream({"name": "[RD download]\n1080p"}).cached is False
    assert quality.parse_stream({"name": "Torrentio\n1080p"}).cached is False


# --- language- and source-aware scoring ------------------------------------

_CAPS_HW = Caps(codecs=frozenset({"h264", "hevc", "hevc10"}), max_resolution=2160, vaapi=True)

S_1080_ITA: Stream = {
    "name": "Torrentio\n1080p",
    "title": "Movie.2024.1080p.BluRay.x264.ITA-GRP\n👤 50 💾 8.0 GB ⚙️ x",
}
S_1080_UNTAGGED: Stream = {
    "name": "Torrentio\n1080p",
    "title": "Movie.2024.1080p.BluRay.x264-GRP\n👤 50 💾 8.0 GB ⚙️ x",
}
S_1080_WEBRIP: Stream = {
    "name": "Torrentio\n1080p",
    "title": "Movie.2024.1080p.WEBRip.x264-GRP\n👤 50 💾 8.0 GB ⚙️ x",
}
S_1080_CACHED_UNTAGGED: Stream = {
    "name": "[RD+] Torrentio\n1080p",
    "title": "Movie.2024.1080p.BluRay.x264-GRP\n👤 50 💾 8.0 GB ⚙️ x",
}


def test_score_prefers_audio_language_at_equal_resolution():
    # Same res/source/seeders/size: the file tagged with a preferred language wins, so
    # mpv finds the wanted track instead of falling back to an untagged (often English) one.
    spec = FilterSpec(audio_langs=("ita", "eng"))
    playable, _ = quality.rank_streams([S_1080_UNTAGGED, S_1080_ITA], _CAPS_HW, spec)
    assert playable[0].stream is S_1080_ITA


def test_score_no_language_preference_is_neutral():
    # Without audio_langs the language term is neutral → order falls back to other terms
    # (here identical), so both stay playable and the tagged one isn't artificially boosted.
    spec = FilterSpec()
    playable, _ = quality.rank_streams([S_1080_UNTAGGED, S_1080_ITA], _CAPS_HW, spec)
    assert {r.stream["title"] for r in playable} == {
        S_1080_UNTAGGED["title"],
        S_1080_ITA["title"],
    }


def test_score_prefers_better_source_at_equal_resolution():
    spec = FilterSpec()
    playable, _ = quality.rank_streams([S_1080_WEBRIP, S_1080_ITA], _CAPS_HW, spec)
    assert playable[0].stream is S_1080_ITA  # BluRay > WEBRip


def test_cached_still_dominates_language():
    # A cached untagged 1080p outranks a non-cached preferred-language 1080p: instant
    # availability stays the top priority (language only breaks ties below cached+res).
    spec = FilterSpec(audio_langs=("ita",))
    playable, _ = quality.rank_streams([S_1080_ITA, S_1080_CACHED_UNTAGGED], _CAPS_HW, spec)
    assert playable[0].stream is S_1080_CACHED_UNTAGGED


def test_parse_dcprip_and_tscr_are_camrip():
    # Cinema leaks (DCP rip, HD-TeleSync-Screener) must be classed as camrip, not unknown.
    assert quality.parse_stream({"title": "Movie 2026 1080p DCPRip x264"}).source == "dcp"
    assert quality.parse_stream({"title": "Movie (2026) HdTScr Lat"}).source == "scr"


def test_rank_excludes_dcprip_cinema_leak():
    # Real regression: a DCPRip tagged ENG was auto-picked over web releases. It must be
    # excluded as a camrip (and never become the auto-pick).
    leak: Stream = {
        "name": "[RD+] Torrentio\n1080p",
        "title": "The Devil Wears Prada 2 [2026 DCPRip] ENG RUS\n👤 50 💾 11.0 GB ⚙️ x",
    }
    web: Stream = {
        "name": "Torrentio\n1080p",
        "title": "Movie.2026.1080p.WEB-DL.x264-GRP\n👤 50 💾 5.0 GB ⚙️ x",
    }
    spec = FilterSpec(audio_langs=("ita", "eng"), exclude_camrip=True)
    playable, excluded = quality.rank_streams([leak, web], _CAPS_HW, spec)
    assert playable and playable[0].stream is web
    assert any("camrip (dcp)" in (r.reason or "") for r in excluded)


def test_score_components_in_sync_with_score():
    info = quality.parse_stream(S_1080_ITA)
    comp = quality.score_components(info, ("ita",))
    assert tuple(comp.values()) == quality._score(info, ("ita",))
    assert comp["lang"] == 2 and comp["source"] == 5  # preferred lang, bluray
