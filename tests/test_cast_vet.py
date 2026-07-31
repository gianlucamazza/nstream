"""Unit tests for cast-path stream vetting (`cast_vet`).

Audio plan, video codec (ADR 0017), container (ADR 0022), and per-invocation quality
parity for cast reselects (ADR 0021). Patches target `cast_vet` for policy helpers and
`stream_select` for shared ranking/URL helpers (`_cast_playable`, …).
"""

from __future__ import annotations

from nstream import cast_vet, stream_select
from nstream.config import Config
from nstream.tracks import Track
from nstream.types import Stream

# --- cast audio-language enforcement (vet_cast_audio) ----------------------


def _ccfg() -> Config:
    return Config(torrentio_base="t", primary_lang="ita", audio_langs=["ita", "eng"])


def _plan(audio, target="ita"):
    return cast_vet._cast_plan_for({"url": "u"}, list(audio), target)


def test_cast_plan_direct_when_first_track_is_target_decodable():
    p = _plan([Track(1, "ita", "aac"), Track(2, "eng", "aac")])
    assert p.mode == "direct" and p.audio_index == 0 and p.real_lang == "ita" and p.verified


def test_cast_plan_remux_when_first_track_target_but_undecodable():
    # Italian is the first track but AC-3 → DMR can't decode → remux track 0 to AAC.
    p = _plan([Track(1, "ita", "ac3", 6), Track(2, "eng", "ac3", 6)])
    assert p.mode == "remux" and p.audio_index == 0 and p.real_lang == "ita"


def test_cast_plan_remux_selects_nondefault_target_track():
    # Default track is English; Italian is buried at index 3 → remux selects it.
    audio = [
        Track(1, "eng", "dts", 6),
        Track(2, "spa", "eac3", 6),
        Track(3, "fra", "aac"),
        Track(4, "ita", "eac3", 6),
    ]
    p = _plan(audio)
    assert p.mode == "remux" and p.audio_index == 3 and p.real_lang == "ita"


def test_cast_plan_remux_selects_nondefault_aac_track():
    # Italian present as a non-default AAC track → still remux (DMR plays track 0) but copy-able.
    p = _plan([Track(1, "eng", "aac"), Track(2, "ita", "aac")])
    assert p.mode == "remux" and p.audio_index == 1


def test_cast_plan_absent_when_no_target_track():
    p = _plan([Track(1, "eng", "aac"), Track(2, "fra", "aac")])
    assert p.mode == "absent" and p.real_lang == "eng"
    assert p.needs_remux is False  # decodable AAC fallback → direct cast is fine


def test_cast_plan_absent_dolby_track_still_needs_remux():
    # Root cause of silent audio: target (ita) absent, fallback dub's first track is E-AC3.
    # An `absent` plan must still flag a remux — a direct cast of Dolby goes out silent on the
    # Default Media Receiver. (Regression for I.S.S.: untagged-language Blu-Ray, eng E-AC3.)
    p = _plan([Track(1, "eng", "eac3", 6), Track(2, "fra", "ac3", 6)])
    assert p.mode == "absent" and p.real_lang == "eng"
    assert p.needs_remux is True


def test_cast_plan_unprobeable_is_direct_unverified():
    p = _plan([])
    assert p.mode == "direct" and p.verified is False


def test_cast_plan_no_preference_is_codec_only():
    assert _plan([Track(1, "eng", "eac3", 6)], target="").mode == "remux"
    assert _plan([Track(1, "eng", "aac")], target="").mode == "direct"


def test_vet_cast_audio_returns_plan_without_reselect_when_present(monkeypatch):
    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", lambda cfg, s: [Track(1, "ita", "ac3", 6)])
    called = []
    monkeypatch.setattr(
        cast_vet, "_reselect_cast_for_lang", lambda *a, **k: called.append(1) or None
    )
    plan = cast_vet.vet_cast_audio(_ccfg(), [], {"url": "u"}, "ita")
    assert plan.mode == "remux" and called == []  # chosen had it → no reselect


def test_vet_cast_audio_reselects_when_chosen_lacks_target(monkeypatch):
    chosen: Stream = {"url": "eng-only"}
    alt: Stream = {"url": "ita-rel"}

    def tracks_of(cfg, s):
        return [Track(1, "eng", "aac")] if s is chosen else [Track(1, "ita", "aac")]

    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", tracks_of)
    monkeypatch.setattr(
        stream_select,
        "_cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(alt, frozenset({"ita"}))],
    )
    plan = cast_vet.vet_cast_audio(_ccfg(), [chosen, alt], chosen, "ita")
    assert plan.mode == "direct" and plan.stream is alt and plan.real_lang == "ita"


def test_vet_cast_audio_absent_when_nobody_has_target(monkeypatch):
    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", lambda cfg, s: [Track(1, "eng", "aac")])
    monkeypatch.setattr(
        stream_select, "_cast_playable", lambda cfg, results, exact_resolution=0: []
    )
    plan = cast_vet.vet_cast_audio(_ccfg(), [{"url": "u"}], {"url": "u"}, "ita")
    assert plan.mode == "absent"


class _R:
    """Minimal RankedStream stand-in (stream + name-tag languages) for reselect tests."""

    def __init__(self, stream, languages):
        self.stream = stream
        self.info = type("I", (), {"languages": languages})()


def test_cast_plan_all_und_tracks_benefit_of_the_doubt():
    """Every track `und`: mirror the local guard (unverifiable → cast it) instead of
    declaring the dub absent — the single mistagged track was often already right."""
    p = _plan([Track(1, "und", "aac")])
    assert p.mode == "direct" and p.verified is False and p.real_lang == "ita"
    p = _plan([Track(1, "und", "ac3")])
    assert p.mode == "remux" and p.verified is False  # codec still decides the tier


def test_reselect_prefers_tagged_unverified_over_wrong_language(monkeypatch):
    """The Dexter: Resurrection bug (2026-07-14): the top pick is a verified WRONG-language
    stream (4K eng/rus) and every ita-tagged release is non-cached → unprobeable (empty
    tracks → unverified direct). Reselect must return the name-tagged-ita release on benefit
    of the doubt, NOT give up and let the caller cast the confirmed-Russian pick. This makes
    the default cast consistent with the forced --audio-lang path (pick_audio_stream_verified),
    which already accepts an unverified name tag."""
    wrong: Stream = {"url": "rus-4k"}
    ita_tagged: Stream = {"url": "ita-webrip"}

    def tracks_of(cfg, s):
        # the wrong pick probes fine (rus first); the ita release is unprobeable
        return [Track(1, "rus", "eac3"), Track(2, "eng", "eac3")] if s is wrong else []

    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", tracks_of)
    monkeypatch.setattr(
        stream_select, "_cast_playable",
        lambda cfg, results, exact_resolution=0: [
            _R(wrong, frozenset({"eng", "rus"})),
            _R(ita_tagged, frozenset({"eng", "ita"})),  # name explicitly claims ita
        ],
    )  # fmt: skip
    plan = cast_vet.vet_cast_audio(_ccfg(), [wrong, ita_tagged], wrong, "ita")
    assert plan.mode == "direct" and plan.stream is ita_tagged
    assert plan.real_lang == "ita" and plan.verified is False  # honest: tagged, not confirmed


def test_reselect_multi_unprobeable_not_trusted_as_target(monkeypatch):
    """An unprobeable release tagged only `multi` (not the target language) is NOT a
    benefit-of-the-doubt match — `multi` doesn't promise ita specifically. With no better
    option, reselect returns None and the caller keeps the (absent) original plan."""
    wrong: Stream = {"url": "rus-4k"}
    multi: Stream = {"url": "multi-rel"}
    monkeypatch.setattr(
        cast_vet, "_cast_audio_tracks",
        lambda cfg, s: [Track(1, "rus", "eac3")] if s is wrong else [],
    )  # fmt: skip
    monkeypatch.setattr(
        stream_select, "_cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(wrong, frozenset({"rus"})), _R(multi, frozenset({"multi"}))],
    )  # fmt: skip
    assert cast_vet._reselect_cast_for_lang(_ccfg(), [], wrong, "ita") is None


def test_reselect_verified_direct_beats_tagged_guess(monkeypatch):
    """A verified ita release must win over an earlier unprobeable ita-tagged guess even if
    the guess is ranked higher — confidence beats a name tag."""
    wrong: Stream = {"url": "rus"}
    guess: Stream = {"url": "ita-guess"}  # higher-ranked, unprobeable
    real: Stream = {"url": "ita-real"}  # lower-ranked, verified ita

    def tracks_of(cfg, s):
        if s is wrong:
            return [Track(1, "rus", "aac")]
        return [] if s is guess else [Track(1, "ita", "aac")]

    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", tracks_of)
    monkeypatch.setattr(
        stream_select, "_cast_playable",
        lambda cfg, results, exact_resolution=0: [
            _R(wrong, frozenset({"rus"})),
            _R(guess, frozenset({"ita"})),
            _R(real, frozenset({"ita"})),
        ],
    )  # fmt: skip
    plan = cast_vet._reselect_cast_for_lang(_ccfg(), [], wrong, "ita")
    assert plan is not None and plan.stream is real and plan.verified is True


# --- cast video-codec vetting (vet_cast_video, ADR 0017) ---------------------


def _video_env(monkeypatch, codecs, candidates=()):
    """Wire the probe seam: `codecs` maps url → probed video codec ("" = unprobeable);
    `candidates` are the ranked cast-playable alternatives (as _R stand-ins)."""
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks",
        lambda url: stream_select.tracks.Tracks(video_codec=codecs.get(url, "")),
    )  # fmt: skip
    monkeypatch.setattr(
        stream_select, "_cast_playable", lambda cfg, results, exact_resolution=0: list(candidates)
    )


def test_vet_cast_video_passes_supported_and_unknown(monkeypatch):
    good: Stream = {"url": "good"}
    unknown: Stream = {"url": "nope"}
    _video_env(monkeypatch, {"good": "h264"})
    assert cast_vet.vet_cast_video(_ccfg(), [], good) == (good, "")
    # Unprobeable keeps the benefit of the doubt, mirroring the audio vetting's stance.
    assert cast_vet.vet_cast_video(_ccfg(), [], unknown) == (unknown, "")


def test_vet_cast_video_reselects_castable_candidate(monkeypatch, capsys):
    bad: Stream = {"url": "divx"}
    alt: Stream = {"url": "h264-rel"}
    _video_env(monkeypatch, {"divx": "mpeg4", "h264-rel": "h264"}, [_R(alt, frozenset())])
    assert cast_vet.vet_cast_video(_ccfg(), [bad, alt], bad) == (alt, "")
    assert "MPEG4" in capsys.readouterr().err


def test_vet_cast_video_reports_codec_when_no_candidate(monkeypatch):
    """Nothing castable: the caller gets the codec verdict (mirror fallback or explicit
    failure) — never a silent black cast."""
    bad: Stream = {"url": "divx"}
    worse: Stream = {"url": "vc1-rel"}
    _video_env(monkeypatch, {"divx": "mpeg4", "vc1-rel": "vc1"}, [_R(worse, frozenset())])
    assert cast_vet.vet_cast_video(_ccfg(), [bad, worse], bad) == (bad, "mpeg4")


def test_vet_cast_video_respects_probe_cap(monkeypatch):
    bad: Stream = {"url": "divx"}
    dead = [_R({"url": f"u{i}"}, frozenset()) for i in range(6)]
    probed: list[str] = []
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks",
        lambda url: probed.append(url)
        or stream_select.tracks.Tracks(video_codec="mpeg4"),
    )  # fmt: skip
    monkeypatch.setattr(
        stream_select, "_cast_playable", lambda cfg, results, exact_resolution=0: dead
    )
    stream, verdict = cast_vet.vet_cast_video(_ccfg(), [], bad, probe_cap=2)
    assert (stream, verdict) == (bad, "mpeg4")
    assert len(probed) == 3  # chosen + exactly probe_cap candidates


def test_reselect_for_lang_skips_undecodable_video(monkeypatch):
    """Live regression (Coherence, 2026-07-16): the ITA-dub reselect picked a cached DivX
    rip the DMR renders as a black screen. A candidate whose probed video the receiver
    can't decode is not a candidate — silent-wrong-language must not become black-screen."""
    chosen: Stream = {"url": "eng-only"}
    divx: Stream = {"url": "divx-ita"}

    def probe(url):
        if url == "divx-ita":
            return stream_select.tracks.Tracks(audio=[Track(1, "ita", "aac")], video_codec="mpeg4")
        return stream_select.tracks.Tracks(audio=[Track(1, "eng", "aac")], video_codec="h264")

    monkeypatch.setattr(stream_select.tracks, "probe_tracks", probe)
    monkeypatch.setattr(
        stream_select, "_cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(divx, frozenset({"ita"}))],
    )  # fmt: skip
    plan = cast_vet.vet_cast_audio(_ccfg(), [chosen, divx], chosen, "ita")
    assert plan.stream is chosen and plan.mode == "absent"  # fallback + safety subs, not black


# --- cast container vetting (vet_cast_container, ADR 0022) --------------------


def _container_env(monkeypatch, probes, candidates=()):
    """Wire the probe seam for container vetting: `probes` maps url → (ffprobe format_name,
    video_codec[, audio_tracks]). `candidates` are the ranked cast-playable alternatives."""

    def probe(url):
        fmt, vc, *rest = probes.get(url, ("", "", []))
        return stream_select.tracks.Tracks(
            container=fmt, video_codec=vc, audio=list(rest[0]) if rest else []
        )

    monkeypatch.setattr(stream_select.tracks, "probe_tracks", probe)
    monkeypatch.setattr(
        stream_select, "_cast_playable", lambda cfg, results, exact_resolution=0: list(candidates)
    )


def test_vet_cast_container_passes_castable(monkeypatch):
    """mp4/webm/unknown containers cast directly (no rewrap flag)."""
    mp4: Stream = {"url": "http://x/a.mp4"}
    webm: Stream = {"url": "http://x/a.webm"}
    unknown: Stream = {"url": "http://x/resolve/id"}  # no extension, probe empty
    _container_env(monkeypatch, {"http://x/a.mp4": ("mov,mp4,m4a,3gp,3g2,mj2", "hevc")})
    assert cast_vet.vet_cast_container(_ccfg(), [], mp4, "ita") == (mp4, False)
    assert cast_vet.vet_cast_container(_ccfg(), [], webm, "ita") == (webm, False)
    assert cast_vet.vet_cast_container(_ccfg(), [], unknown, "ita") == (unknown, False)


def test_vet_cast_container_reselects_verified_target_mp4_twin(monkeypatch, capsys):
    """An MKV pick swaps only to an MP4 that is a VERIFIED direct cast in the target language
    (its real first track is ita/aac) — a free direct cast, no rewrap."""
    mkv: Stream = {"url": "http://x/a.mkv"}
    mp4: Stream = {"url": "http://x/b.mp4"}
    _container_env(
        monkeypatch,
        {
            "http://x/a.mkv": ("matroska,webm", "hevc"),
            "http://x/b.mp4": ("mov,mp4,m4a", "hevc", [Track(1, "ita", "aac")]),
        },
        [_R(mp4, frozenset({"ita"}))],
    )
    assert cast_vet.vet_cast_container(_ccfg(), [mkv, mp4], mkv, "ita") == (mp4, False)
    assert "MP4" in capsys.readouterr().err


def test_vet_cast_container_keeps_mkv_over_wrong_language_multi_mp4(monkeypatch):
    """Regression (Independence Day, ita→spa): a name-`multi` MP4 whose REAL first track is
    Spanish must NOT preempt the Italian rewrap. Keep the mkv (→ rewrap, ita selected later)."""
    mkv: Stream = {"url": "http://x/a.mkv"}
    multi_mp4: Stream = {"url": "http://x/b.mp4"}
    _container_env(
        monkeypatch,
        {
            "http://x/a.mkv": ("matroska,webm", "hevc"),
            "http://x/b.mp4": (
                "mov,mp4,m4a",
                "hevc",
                [Track(1, "spa", "aac"), Track(2, "eng", "aac")],
            ),
        },
        [_R(multi_mp4, frozenset({"multi", "spa"}))],
    )
    assert cast_vet.vet_cast_container(_ccfg(), [mkv, multi_mp4], mkv, "ita") == (mkv, True)


def test_vet_cast_container_no_twin_flags_rewrap(monkeypatch):
    """No compatible-container candidate → keep the mkv and return True (caller rewraps)."""
    mkv: Stream = {"url": "http://x/a.mkv"}
    _container_env(monkeypatch, {"http://x/a.mkv": ("matroska,webm", "hevc")})
    assert cast_vet.vet_cast_container(_ccfg(), [mkv], mkv, "ita") == (mkv, True)


def test_vet_cast_container_skips_bad_video_candidate(monkeypatch):
    """An mp4 twin whose video the DMR can't render is not a candidate (would trade a
    rewrap for a black screen) → fall back to the rewrap flag."""
    mkv: Stream = {"url": "http://x/a.mkv"}
    mp4_divx: Stream = {"url": "http://x/b.mp4"}
    _container_env(
        monkeypatch,
        {"http://x/a.mkv": ("matroska,webm", "hevc"), "http://x/b.mp4": ("mov,mp4,m4a", "mpeg4")},
        [_R(mp4_divx, frozenset({"ita"}))],
    )
    assert cast_vet.vet_cast_container(_ccfg(), [mkv, mp4_divx], mkv, "ita") == (mkv, True)


def test_vet_cast_container_lying_mp4_extension(monkeypatch):
    """A .mp4 that ffprobe reveals as Matroska is treated INCOMPATIBLE (rewrap), not black cast."""
    liar: Stream = {"url": "http://x/a.mp4"}
    _container_env(monkeypatch, {"http://x/a.mp4": ("matroska,webm", "hevc")})
    assert cast_vet.vet_cast_container(_ccfg(), [liar], liar, "ita") == (liar, True)


# --- per-invocation constraint parity (ADR 0021) -----------------------------


def _r_res(url: str, langs: frozenset[str], res: int):
    r = _R({"url": url}, langs)
    r.info.resolution = res
    return r


def test_reselect_for_lang_honors_exact_resolution(monkeypatch):
    """THE parity defect (3 field incidents in one day): the language reselect must not
    return a release the user's --quality excluded. The filter is applied by
    _cast_playable itself; here we pin that the exact value REACHES it."""
    seen = {}

    def fake_playable(cfg, results, exact_resolution=0):
        seen["exact"] = exact_resolution
        return []

    monkeypatch.setattr(stream_select, "_cast_playable", fake_playable)
    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", lambda cfg, s: [])
    cast_vet._reselect_cast_for_lang(_ccfg(), [], {"url": "x"}, "ita", exact_resolution=1080)
    assert seen["exact"] == 1080


def test_vet_cast_audio_threads_exact_to_reselect(monkeypatch):
    seen = {}
    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", lambda cfg, s: [Track(1, "eng", "aac")])
    monkeypatch.setattr(
        cast_vet, "_reselect_cast_for_lang",
        lambda cfg, results, cur, lang, exact_resolution=0: seen.update(exact=exact_resolution)
        or None,
    )  # fmt: skip
    cast_vet.vet_cast_audio(_ccfg(), [], {"url": "u"}, "ita", exact_resolution=1080)
    assert seen["exact"] == 1080


def test_vet_cast_video_threads_exact(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks",
        lambda url: stream_select.tracks.Tracks(video_codec="mpeg4"),
    )  # fmt: skip
    monkeypatch.setattr(
        stream_select, "_cast_playable",
        lambda cfg, results, exact_resolution=0: seen.update(exact=exact_resolution) or [],
    )  # fmt: skip
    cast_vet.vet_cast_video(_ccfg(), [], {"url": "divx"}, exact_resolution=720)
    assert seen["exact"] == 720


def test_cast_resolver_and_languages_thread_exact(monkeypatch):
    seen = []
    monkeypatch.setattr(
        stream_select, "_playable_set",
        lambda cfg, results, *, cast, exact_resolution=0: seen.append(exact_resolution) or [],
    )  # fmt: skip
    cast_vet.cast_languages(_ccfg(), [], exact_resolution=1080)
    cast_vet.cast_resolver(_ccfg(), [], exact_resolution=1080)
    assert seen == [1080, 1080]


# --- cast_languages / cast_resolver (ranking + remux cap) -------------------

_S_ITA: Stream = {
    "url": "http://ita",
    "name": "[RD+] Torrentio\n1080p",
    "title": "Film.2020.iTA.1080p.BluRay.DDP5.1.x264-GRP\n👤 20 💾 8.0 GB ⚙️ x",
}
_S_ENG_REMUX: Stream = {
    "url": "http://eng-remux",
    "name": "[RD+] Torrentio\n4k",
    "title": "Film.2020.ENG.2160p.UHD.BluRay.REMUX.TrueHD-GRP\n👤 30 💾 60.0 GB ⚙️ x",
}
_S_ENG_WEBDL: Stream = {
    "url": "http://eng-webdl",
    "name": "[RD+] Torrentio\n1080p",
    "title": "Film.2020.ENG.1080p.WEB-DL.DDP5.1.x264-GRP\n👤 10 💾 6.0 GB ⚙️ x",
}


def test_cast_languages_lists_compatible():
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    langs = cast_vet.cast_languages(cfg, [_S_ITA, _S_ENG_REMUX, _S_ENG_WEBDL])
    assert langs == ("ita", "eng")  # preferred order; eng present via the WEB-DL


def test_cast_resolver_picks_compatible_release():
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    resolve = cast_vet.cast_resolver(cfg, [_S_ITA, _S_ENG_REMUX, _S_ENG_WEBDL])
    # ITA → the ITA release; missing → None. For ENG both releases carry Dolby audio the
    # DMR can't decode (TrueHD / DDP), so both are castable via Tier-2 remux (default on).
    # The remux resolution cap (default 1080p) then prefers the 1080p WEB-DL over the 4K
    # TrueHD remux — a 4K remux would download tens of GB; the 1080p one is far cheaper.
    assert resolve("ita") == "http://ita"
    assert resolve("eng") == "http://eng-webdl"
    assert resolve("ger") is None


def test_cast_resolver_excludes_lossless_without_remux():
    # With Tier-2 remux disabled, the old behaviour holds: TrueHD is dropped as unplayable,
    # so ENG resolves to the (E-AC-3) WEB-DL instead of the 4K TrueHD remux.
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"], cast_remux=False)
    resolve = cast_vet.cast_resolver(cfg, [_S_ITA, _S_ENG_REMUX, _S_ENG_WEBDL])
    assert resolve("eng") == "http://eng-webdl"
