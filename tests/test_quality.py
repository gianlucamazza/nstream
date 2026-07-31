"""Unit tests for hardware-aware stream parsing, caps detection and ranking."""

from __future__ import annotations

from nstream import quality
from nstream.config import Config
from nstream.quality import FilterSpec, HwCaps
from nstream.types import Stream

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

CAPS = HwCaps(codecs=frozenset({"h264", "hevc", "hevc10", "vp9", "av1"}), max_resolution=2160)


def test_reason_camrip():
    info = quality.parse_stream(
        {"name": "Torrentio\n720p", "title": "F.2025.720p.CAM-X\n👤 5 💾 1 GB"}
    )
    spec = FilterSpec(max_resolution=2160, exclude_camrip=True)
    assert quality.unsupported_reason(info, CAPS, spec) == "camrip (cam)"


def test_reason_exact_resolution():
    """Per-session quality filter: keep only the exact resolution; unknown drops out."""
    i1080 = quality.parse_stream(S_1080)
    i4k = quality.parse_stream(S_WEBDL_4K)
    unk = quality.parse_stream({"name": "Torrentio\n?", "title": "F.WEB-DL\n👤 9"})
    spec = FilterSpec(max_resolution=2160, exact_resolution=1080)
    assert quality.unsupported_reason(i1080, CAPS, spec) is None
    assert quality.unsupported_reason(i4k, CAPS, spec) == "2160p"
    assert quality.unsupported_reason(unk, CAPS, spec) == "res ?"


def test_rank_exact_resolution_only_1080():
    """exact_resolution hard-filters before score; only matching res stays playable."""
    playable, excluded = _rank([S_WEBDL_4K, S_1080, S_8K], max_resolution=4320)
    # Without exact filter, 4K/8K may rank; with exact 1080 only the 1080 stream remains.
    spec = FilterSpec(max_resolution=4320, exact_resolution=1080)
    playable, excluded = quality.rank_streams([S_WEBDL_4K, S_1080, S_8K], CAPS, spec)
    assert len(playable) == 1
    assert playable[0].info.resolution == 1080
    assert {r.info.resolution for r in excluded} >= {2160, 4320}


def test_parse_quality_aliases():
    assert quality.parse_quality("auto") == 0
    assert quality.parse_quality("4k") == 2160
    assert quality.parse_quality("1080p") == 1080
    assert quality.parse_quality("fhd") == 1080
    assert quality.parse_quality("720") == 720
    assert quality.parse_quality("hd") == 720
    assert quality.parse_quality("480p") == 480
    assert quality.parse_quality("nope") is None
    assert quality.parse_quality("") is None


def test_resolutions_of():
    from nstream.quality import RankedStream, StreamInfo

    rows = [
        RankedStream({}, StreamInfo(resolution=2160)),
        RankedStream({}, StreamInfo(resolution=1080)),
        RankedStream({}, StreamInfo(resolution=1080)),
        RankedStream({}, StreamInfo(resolution=0)),
    ]
    assert quality.resolutions_of(rows) == [2160, 1080]


def test_reason_language():
    info = quality.parse_stream(
        {"name": "Torrentio\n1080p", "title": "F.2025.FRENCH.1080p.WEB-DL\n👤 9 💾 2 GB"}
    )
    spec = FilterSpec(max_resolution=2160, audio_langs=("ita", "eng"), lang_filter=True)
    assert quality.unsupported_reason(info, CAPS, spec) == "lingua fra"


def test_reason_language_czech_tagged():
    """Regression: Czech (and CZ/SK-tracker friends) used to be absent from the registry,
    so a Czech-tagged release parsed as untagged and slipped past the language filter."""
    info = quality.parse_stream(
        {"name": "Torrentio\n1080p", "title": "F.2015.CZ.1080p.WEB-DL\n👤 9 💾 2 GB"}
    )
    assert "ces" in info.languages
    spec = FilterSpec(max_resolution=2160, audio_langs=("ita", "eng"), lang_filter=True)
    assert quality.unsupported_reason(info, CAPS, spec) == "lingua ces"


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
    assert quality.preferred_hwdec(HwCaps(vaapi=True)) == "vaapi"
    assert quality.preferred_hwdec(HwCaps(vaapi=False)) is None


# --- ranking ---------------------------------------------------------------

CAPS_NO_AV1 = HwCaps(codecs=frozenset({"h264", "hevc", "hevc10", "vp9"}), max_resolution=2160)


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


# --- Tier-2 remux resolution cap -------------------------------------------

_C_4K_DOLBY: Stream = {
    "name": "[RD+] Torrentio\n4k",
    "title": "Film.2024.2160p.UHD.BluRay.HEVC.TrueHD.7.1-GRP\n👤 20 💾 60.0 GB ⚙️ x",
}
_C_1080_DOLBY: Stream = {
    "name": "[RD+] Torrentio\n1080p",
    "title": "Film.2024.1080p.BluRay.x264.AC3-GRP\n👤 20 💾 8.0 GB ⚙️ x",
}
_C_4K_AAC: Stream = {
    "name": "[RD+] Torrentio\n4k",
    "title": "Film.2024.2160p.WEB-DL.HEVC.AAC-GRP\n👤 20 💾 10.0 GB ⚙️ x",
}
_C_1080_AAC: Stream = {
    "name": "[RD+] Torrentio\n1080p",
    "title": "Film.2024.1080p.WEB-DL.x264.AAC-GRP\n👤 20 💾 6.0 GB ⚙️ x",
}


# A 4K REMUX whose name omits the audio codec — reads as audio="" (unknown) yet a remux
# carries the lossless disc track, so it really needs a huge host remux. The field trap.
_C_4K_REMUX_UNLABELED: Stream = {
    "name": "[RD+] Torrentio\n4k",
    "title": "Film.2024.2160p.UHD.BluRay.REMUX.HEVC.HDR-GRP\n👤 20 💾 86.0 GB ⚙️ x",
}


def _cast_spec(cap: int, size: int = 0) -> FilterSpec:
    return FilterSpec(
        max_resolution=2160,
        cast_audio=True,
        cast_remux=True,
        cast_remux_max_resolution=cap,
        cast_remux_max_size=size,
    )


def test_remux_cap_prefers_1080_dolby_over_4k_dolby():
    # Both need a remux (TrueHD / AC-3) → cap prefers the cheaper 1080p fetch over a 4K one.
    playable, _ = quality.rank_streams(
        [_C_4K_DOLBY, _C_1080_DOLBY], quality.cast_caps(), _cast_spec(1080)
    )
    assert playable[0].stream is _C_1080_DOLBY


def test_remux_cap_does_not_override_native_aac():
    # A direct 4K AAC cast (free, no host download) still beats a 1080p Dolby remux.
    playable, _ = quality.rank_streams(
        [_C_4K_AAC, _C_1080_DOLBY], quality.cast_caps(), _cast_spec(1080)
    )
    assert playable[0].stream is _C_4K_AAC


def test_remux_cap_keeps_sole_4k_dolby():
    # Preference, not exclusion: the only Dolby release (4K) is still playable/chosen.
    playable, _ = quality.rank_streams([_C_4K_DOLBY], quality.cast_caps(), _cast_spec(1080))
    assert len(playable) == 1 and playable[0].stream is _C_4K_DOLBY


def test_remux_cap_zero_disables():
    # cap=0 → no remux cap → 4K Dolby wins on resolution again.
    playable, _ = quality.rank_streams(
        [_C_4K_DOLBY, _C_1080_DOLBY], quality.cast_caps(), _cast_spec(0)
    )
    assert playable[0].stream is _C_4K_DOLBY


def test_remux_cap_leaves_direct_aac_casts_untouched():
    # AAC needs no remux, so the cap never applies: the 4K AAC still wins over 1080p AAC.
    playable, _ = quality.rank_streams(
        [_C_4K_AAC, _C_1080_AAC], quality.cast_caps(), _cast_spec(1080)
    )
    assert playable[0].stream is _C_4K_AAC


def test_likely_needs_remux():
    likely = quality._likely_needs_remux
    assert likely(quality.StreamInfo(audio="ac3")) is True
    assert likely(quality.StreamInfo(audio="truehd")) is True
    assert likely(quality.StreamInfo(audio="", source="remux")) is True  # unlabelled remux
    assert likely(quality.StreamInfo(audio="aac")) is False
    assert likely(quality.StreamInfo(audio="", source="webdl")) is False  # unlabelled web → benefit


def test_size_budget_demotes_oversized_unlabelled_remux():
    # The field trap: an 86GB unlabelled 4K REMUX must not out-rank a feasible 1080p AC-3 (8GB)
    # once a size budget is set — size is the real download cost the resolution cap can't see.
    playable, _ = quality.rank_streams(
        [_C_4K_REMUX_UNLABELED, _C_1080_DOLBY], quality.cast_caps(), _cast_spec(1080, size=20)
    )
    assert playable[0].stream is _C_1080_DOLBY


def test_size_budget_keeps_sole_oversized_remux():
    # Preference, not exclusion: a sole oversized remux is still playable (cast-time guard then
    # prompts before the download).
    playable, _ = quality.rank_streams(
        [_C_4K_REMUX_UNLABELED], quality.cast_caps(), _cast_spec(1080, size=20)
    )
    assert len(playable) == 1 and playable[0].stream is _C_4K_REMUX_UNLABELED


def test_size_budget_does_not_demote_aac():
    # A 4K AAC (no remux, streamed directly) is never demoted by the size budget, even when big.
    big_aac: Stream = {
        "name": "[RD+] Torrentio\n4k",
        "title": "Film.2024.2160p.WEB-DL.HEVC.AAC-GRP\n👤 20 💾 40.0 GB ⚙️ x",
    }
    playable, _ = quality.rank_streams(
        [big_aac, _C_1080_DOLBY], quality.cast_caps(), _cast_spec(1080, size=20)
    )
    assert playable[0].stream is big_aac


def test_size_budget_zero_disables():
    # size=0 → no size demotion → the 4K remux wins on resolution again (cap also off).
    playable, _ = quality.rank_streams(
        [_C_4K_REMUX_UNLABELED, _C_1080_DOLBY], quality.cast_caps(), _cast_spec(0, size=0)
    )
    assert playable[0].stream is _C_4K_REMUX_UNLABELED


def test_cached_marker_provider_agnostic():
    # Torrentio marks instant streams per-debrid: [RD+]/[AD+]/[PM+]/[TB+]/[Putio+].
    for mark in ("[RD+]", "[AD+]", "[PM+]", "[TB+]", "[Putio+]"):
        assert quality.parse_stream({"name": f"{mark} Torrentio\n1080p"}).cached is True
    # Non-cached / no marker → not cached.
    assert quality.parse_stream({"name": "[RD download]\n1080p"}).cached is False
    assert quality.parse_stream({"name": "Torrentio\n1080p"}).cached is False


# --- language- and source-aware scoring ------------------------------------

_CAPS_HW = HwCaps(codecs=frozenset({"h264", "hevc", "hevc10"}), max_resolution=2160, vaapi=True)

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


def test_parse_languages_unchanged_after_registry_move():
    # Regression: the historical 8 languages must parse exactly as before, now that the
    # token/flag maps are derived from languages.LANGUAGES.
    assert quality.parse_stream({"title": "Movie ITA ENG MULTI"}).languages == frozenset(
        {"ita", "eng", "multi"}
    )
    assert quality.parse_stream({"name": "🇮🇹 Torrentio\n1080p"}).languages == frozenset({"ita"})


def test_parse_new_language_jpn():
    assert "jpn" in quality.parse_stream({"title": "Anime.2024.1080p.JPN.x264"}).languages


def test_lat_tagged_spanish_and_demoted():
    # A "LAT" (Latino) release is now tagged spa and demoted by lang_filter for ita/eng.
    lat: Stream = {"name": "[RD+] x\n1080p", "title": "El diablo viste a la moda 2 LAT.mp4"}
    assert "spa" in quality.parse_stream(lat).languages
    spec = FilterSpec(audio_langs=("ita", "eng"), lang_filter=True)
    playable, excluded = quality.rank_streams([lat], _CAPS_HW, spec)
    assert not playable and any("lingua spa" in (r.reason or "") for r in excluded)


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


def test_title_match_demotes_mismapped_torrent():
    # Real regression (I.S.S.): Torrentio returned an unrelated cached "Charlie Brown" pack
    # (untagged → resolution 0). Cached dominates the score, so it auto-won over every real
    # release. With the searched title known, `title_match` sinks the mismatch below the real
    # (uncached) release — without excluding it (still listed/pickable).
    junk: Stream = {
        "name": "[RD+] Torrentio\n",
        "title": "Charlie Brown and Snoopy (Anthology collection in MP4 format) [Landdo18]"
        "\n👤 4 💾 0.22 GB ⚙️ x",
    }
    real: Stream = {
        "name": "Torrentio\n720p",
        "title": "I.S.S..2023.720p.WEBRip [YTS.MX]\n👤 10 💾 0.86 GB ⚙️ x",
    }
    # Without a title the cached junk still wins (documents the pre-fix behaviour).
    playable, _ = quality.rank_streams([junk, real], _CAPS_HW, FilterSpec())
    assert playable[0].stream is junk
    # With the title, the real acronym-titled release ranks first; junk stays playable.
    playable, _ = quality.rank_streams([junk, real], _CAPS_HW, FilterSpec(title="I.S.S."))
    assert playable[0].stream is real
    assert junk in [r.stream for r in playable]


def test_title_matches_helper():
    # Acronym title: compact-substring path (no usable word tokens).
    assert quality._title_matches("I.S.S..2023.720p.WEBRip [YTS.MX]", "I.S.S.")
    assert not quality._title_matches("Charlie Brown and Snoopy Anthology", "I.S.S.")
    # Multi-word title: token-overlap path, tolerant of reordering/dropped words.
    assert quality._title_matches("Dark.Knight.2008.1080p.BluRay", "The Dark Knight")
    assert not quality._title_matches("Frozen.2013.1080p.WEB", "The Dark Knight")
    # No-op when the title is empty or too short to match reliably.
    assert quality._title_matches("anything at all", "")
    assert quality._title_matches("anything at all", "Up")


def test_score_components_in_sync_with_score():
    info = quality.parse_stream(S_1080_ITA)
    comp = quality.score_components(info, ("ita",))
    assert tuple(comp.values()) == quality._score(info, ("ita",))
    assert comp["lang"] == 4 and comp["source"] == 5  # primary lang, bluray


def _lang(title: str, langs: tuple[str, ...]) -> int:
    return quality._lang_rank(quality.parse_stream({"title": title}), langs)


def test_lang_rank_levels():
    pref = ("ita", "eng")
    assert _lang("Film ITA ENG 1080p", pref) == 4  # primary explicit
    assert _lang("Film ENG 1080p", pref) == 3  # fallback explicit, no primary
    assert _lang("Film MULTI 1080p", pref) == 2  # multi/dual: only a maybe
    assert _lang("Film Dual 1080p", pref) == 2  # "Dual" → multi token
    assert _lang("Film 1080p", pref) == 1  # untagged: benefit of the doubt
    assert _lang("Film FRENCH 1080p", pref) == 0  # tagged only non-preferred


def test_dual_does_not_outrank_explicit_primary():
    # Regression: a "Dual" release (which may be e.g. Latino+Eng, no Italian) must not
    # outrank one that explicitly names the primary language at equal quality.
    base = "2160p.BluRay.x265\n👤 20 💾 20 GB"
    dual: Stream = {"name": "[RD+] Torrentio", "title": f"Dune.Part.Two.Dual.{base}"}
    ita: Stream = {"name": "[RD+] Torrentio", "title": f"Dune.Part.Two.ITA.ENG.{base}"}
    spec = FilterSpec(audio_langs=("ita", "eng"))
    playable, _ = quality.rank_streams([dual, ita], _CAPS_HW, spec)
    assert playable[0].stream is ita


# --- cast profile: DMR-honest audio/video ranking (L1) ---------------------


def _mk(name):
    return {"name": name, "title": name, "url": "u"}


def test_cast_prefers_h264_aac_over_4k_hevc_ac3():
    # The Default Media Receiver can't decode AC-3 and HEVC is unreliable → a working
    # 1080p H.264+AAC must outrank a silent 4K HEVC+AC3 for cast.
    streams = [
        _mk("[RD+] T\n2160p\nThe.Matrix.2160p.HEVC.AC3"),
        _mk("[RD+] T\n1080p\nThe.Matrix.1080p.x264.AAC"),
    ]
    spec = quality.FilterSpec.from_config(Config(torrentio_base="tb"), cast_audio=True)
    playable, _ = quality.rank_streams(streams, quality.cast_caps(), spec)
    top = quality.parse_stream(playable[0].stream)
    assert top.codec == "h264" and top.audio == "aac"


def test_local_still_prefers_4k_hevc():
    # Local mpv decodes everything → keep the original resolution/HEVC preference.
    streams = [
        _mk("[RD+] T\n2160p\nThe.Matrix.2160p.HEVC.AC3"),
        _mk("[RD+] T\n1080p\nThe.Matrix.1080p.x264.AAC"),
    ]
    spec = quality.FilterSpec.from_config(Config(torrentio_base="tb"))
    playable, _ = quality.rank_streams(streams, quality.detect_caps(), spec)
    assert quality.parse_stream(playable[0].stream).resolution == 2160


def test_cast_audio_rank_order():
    aac = quality.StreamInfo(audio="aac")
    untagged = quality.StreamInfo(audio="")
    ac3 = quality.StreamInfo(audio="ac3")
    assert quality._cast_audio_rank(aac) > quality._cast_audio_rank(untagged)
    assert quality._cast_audio_rank(untagged) > quality._cast_audio_rank(ac3)


def test_parse_stream_memoized_and_key_complete():
    """parse_stream is cached per (name, title, infoHash, fileIdx): identical inputs share
    one StreamInfo; ANY key field change (incl. fileIdx) re-parses."""
    quality._PARSE_CACHE.clear()
    s1: Stream = {
        "name": "[RD+] X\n1080p",
        "title": "Movie.2024.1080p\n👤 9 💾 8 GB",
        "infoHash": "aa",
    }
    a = quality.parse_stream(s1)
    b = quality.parse_stream(Stream(**s1))  # equal content, different dict → cache hit
    assert a is b
    c = quality.parse_stream({**s1, "fileIdx": 2})
    assert c is not a and c.file_idx == 2
    quality._PARSE_CACHE.clear()


def test_parse_legacy_codecs_named_not_unknown():
    """Pre-2010 rip tokens must parse to a REAL codec name, not "" — the unknown-codec
    benefit of the doubt is what let a DivX rip through the cast filter (ADR 0017)."""
    assert quality._parse_codec("Coherence.2013.iTALiAN.XviD-GRP") == "mpeg4"
    assert quality._parse_codec("Movie.1999.DivX.iTA") == "mpeg4"
    assert quality._parse_codec("Old.Doc.DVDRip.MP4V") == "mpeg4"
    assert quality._parse_codec("Concert.2001.MPEG-2.DVD") == "mpeg2"
    assert quality._parse_codec("Film.2006.VC-1.1080p") == "vc1"
    assert quality._parse_codec("Clip.2005.WMV-HD") == "vc1"


def test_parse_legacy_does_not_shadow_modern_tokens():
    """ "MPEG-4 AVC" (Blu-ray remux naming) is h264; a modern token always wins."""
    assert quality._parse_codec("Movie.BluRay.REMUX.MPEG-4.AVC.DTS-HD") == "h264"
    assert quality._parse_codec("Show.2160p.MPEG-4.HEVC") == "hevc"


def test_cast_filter_excludes_legacy_codec():
    """A named legacy codec is excluded by the cast profile like AV1 (no-HW), instead of
    sailing through as unknown and casting a black screen."""
    info = quality.parse_stream(
        {"name": "[RD+] T", "title": "Coherence.2013.iTALiAN.XviD-GRP\n👤 5 💾 1.37 GB"}
    )
    assert info.codec == "mpeg4"
    reason = quality.unsupported_reason(info, quality.cast_caps(), quality.FilterSpec())
    assert reason == "MPEG4 no-HW"


# --- cast container model (ADR 0022) -----------------------------------------


def test_parse_container_from_behavior_hints_filename():
    """The container comes from `behaviorHints.filename` (where the extension lives),
    not the name+title text `_text` parses."""

    def mk(fn):
        return {
            "name": "[RD+] T",
            "title": "Dune.2024.1080p.HEVC",
            "behaviorHints": {"filename": fn},
        }

    assert quality.parse_stream(mk("Dune.2024.mkv")).container == "mkv"
    assert quality.parse_stream(mk("Dune.2024.mp4")).container == "mp4"
    assert quality.parse_stream(mk("Dune.2024.m4v")).container == "mp4"
    assert quality.parse_stream(mk("Dune.2024.webm")).container == "webm"
    assert quality.parse_stream(mk("Dune.2024.avi")).container == "avi"
    assert quality.parse_stream(mk("Dune.2024")).container == ""  # no extension → unknown


def test_parse_container_falls_back_to_url_extension():
    """With no behaviorHints.filename, the url path tail supplies the extension."""
    assert (
        quality.parse_stream({"name": "t", "title": "t", "url": "http://x/a.mkv"}).container
        == "mkv"
    )
    assert (
        quality.parse_stream({"name": "t", "title": "t", "url": "http://x/a.mp4?tok=1"}).container
        == "mp4"
    )
    assert (
        quality.parse_stream({"name": "t", "title": "t", "url": "http://x/resolve/id"}).container
        == ""
    )


def test_parse_cache_keys_on_container():
    """Two streams identical but for the container must not collide in the parse cache."""
    a = {"name": "T", "title": "Dune", "behaviorHints": {"filename": "d.mkv"}}
    b = {"name": "T", "title": "Dune", "behaviorHints": {"filename": "d.mp4"}}
    assert quality.parse_stream(a).container == "mkv"
    assert quality.parse_stream(b).container == "mp4"


def test_container_from_format_disambiguates_matroska_webm():
    """ffprobe reports "matroska,webm" for BOTH .mkv and .webm — the extension splits them."""
    assert quality.container_from_format("mov,mp4,m4a,3gp,3g2,mj2", "mp4") == "mp4"
    assert quality.container_from_format("matroska,webm", "mkv") == "mkv"
    assert quality.container_from_format("matroska,webm", "webm") == "webm"
    assert quality.container_from_format("matroska,webm", "") == "mkv"  # no ext → assume mkv
    assert quality.container_from_format("avi", "") == "avi"
    assert quality.container_from_format("", "mkv") == ""  # probe failed → caller uses ext


def test_container_castable_and_mime():
    assert quality.container_castable("mp4") and quality.container_castable("webm")
    assert not quality.container_castable("mkv") and not quality.container_castable("avi")
    assert quality.container_castable("")  # unknown → benefit of the doubt
    assert quality.container_mime("mp4") == "video/mp4"
    assert quality.container_mime("webm") == "video/webm"
    assert quality.container_mime("mkv") == ""  # not a direct-cast MIME


def test_likely_needs_remux_counts_bad_container():
    """A 4K mkv reads as remux-likely so it is demoted below an mp4 alternative and the
    resolution/size caps apply (ADR 0022) — an AAC mkv would otherwise look direct-castable."""
    mkv_4k = quality.parse_stream(
        {"name": "T", "title": "Dune.2024.2160p.HEVC.AAC\n👤 9 💾 40 GB",
         "behaviorHints": {"filename": "dune.4k.mkv"}}
    )  # fmt: skip
    mp4_1080 = quality.parse_stream(
        {"name": "T", "title": "Dune.2024.1080p.HEVC.AAC\n👤 9 💾 6 GB",
         "behaviorHints": {"filename": "dune.mp4"}}
    )  # fmt: skip
    assert quality._likely_needs_remux(mkv_4k) is True
    assert quality._likely_needs_remux(mp4_1080) is False
