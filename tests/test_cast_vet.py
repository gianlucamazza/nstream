"""Unit tests for cast-path stream vetting (`cast_vet`).

Audio plan, video codec (ADR 0017), container (ADR 0022), and per-invocation quality
parity for cast reselects (ADR 0021). Patches target `cast_vet` for policy helpers and
`stream_select` for shared ranking/URL helpers (`cast_playable`, …).
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
        "cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(alt, frozenset({"ita"}))],
    )
    plan = cast_vet.vet_cast_audio(_ccfg(), [chosen, alt], chosen, "ita")
    assert plan.mode == "direct" and plan.stream is alt and plan.real_lang == "ita"


def test_vet_cast_audio_absent_when_nobody_has_target(monkeypatch):
    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", lambda cfg, s: [Track(1, "eng", "aac")])
    monkeypatch.setattr(stream_select, "cast_playable", lambda cfg, results, exact_resolution=0: [])
    plan = cast_vet.vet_cast_audio(_ccfg(), [{"url": "u"}], {"url": "u"}, "ita")
    assert plan.mode == "absent"


class _R:
    """Minimal RankedStream stand-in (stream + name-tag languages) for reselect tests."""

    def __init__(self, stream, languages, size_gb=0.0, container=""):
        from types import SimpleNamespace

        self.stream = stream
        self.info = SimpleNamespace(
            languages=languages, resolution=0, size_gb=size_gb, container=container
        )


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
        stream_select, "cast_playable",
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
        stream_select, "cast_playable",
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
        stream_select, "cast_playable",
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
        stream_select, "cast_playable", lambda cfg, results, exact_resolution=0: list(candidates)
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
        stream_select, "cast_playable", lambda cfg, results, exact_resolution=0: dead
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
        stream_select, "cast_playable",
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
        stream_select, "cast_playable", lambda cfg, results, exact_resolution=0: list(candidates)
    )


def test_instant_direct_prefers_primary_mp4_over_english(monkeypatch):
    """Italian AAC in MP4 beats an English AAC MP4, whichever comes first."""
    ita: Stream = {"url": "http://x/ita.mp4"}
    eng: Stream = {"url": "http://x/eng.mp4"}
    _container_env(
        monkeypatch,
        {
            "http://x/ita.mp4": ("mov,mp4,m4a", "hevc", [Track(1, "ita", "aac")]),
            "http://x/eng.mp4": ("mov,mp4,m4a", "hevc", [Track(1, "eng", "aac")]),
        },
        [_R(eng, frozenset({"eng"})), _R(ita, frozenset({"ita"}))],
    )
    plan = cast_vet.find_instant_direct(_ccfg(), [eng, ita], ("ita", "eng"))
    assert plan is not None and plan.stream is ita and plan.mode == "direct"
    assert plan.real_lang == "ita" and plan.verified


def test_instant_direct_english_mp4_when_italian_is_mkv_ac3(monkeypatch):
    """The Lobster shape: Italian AC-3 inside MKV is not instant; English AAC MP4 is."""
    mkv: Stream = {"url": "http://x/ita.mkv", "name": "Film.2015.1080p.AC3.ITA.mkv"}
    eng: Stream = {"url": "http://x/eng.mp4", "name": "Film.2015.1080p.mp4"}
    _container_env(
        monkeypatch,
        {
            "http://x/ita.mkv": (
                "matroska,webm",
                "hevc",
                [Track(1, "ita", "ac3", 6), Track(2, "eng", "aac")],
            ),
            "http://x/eng.mp4": ("mov,mp4,m4a", "hevc", [Track(1, "eng", "aac")]),
        },
        [_R(mkv, frozenset({"ita", "eng"})), _R(eng, frozenset({"eng"}))],
    )
    plan = cast_vet.find_instant_direct(_ccfg(), [mkv, eng], ("ita", "eng"))
    assert plan is not None and plan.stream is eng and plan.real_lang == "eng"
    slow = cast_vet._cast_plan_for(mkv, [Track(1, "ita", "ac3", 6)], "ita")
    notice = cast_vet.instant_defer_notice(slow, plan, "ita")
    assert "cast diretto eng" in notice and "ac3" in notice


def test_instant_direct_none_when_only_mkv(monkeypatch):
    """No MP4/WebM → nothing to start early; the caller keeps the remux."""
    mkv: Stream = {"url": "http://x/ita.mkv"}
    eng: Stream = {"url": "http://x/eng.mkv"}
    _container_env(
        monkeypatch,
        {
            "http://x/ita.mkv": ("matroska,webm", "hevc", [Track(1, "ita", "ac3", 6)]),
            "http://x/eng.mkv": ("matroska,webm", "hevc", [Track(1, "eng", "aac")]),
        },
        [_R(mkv, frozenset({"ita"})), _R(eng, frozenset({"eng"}))],
    )
    assert cast_vet.find_instant_direct(_ccfg(), [mkv, eng], ("ita", "eng")) is None


def test_instant_direct_rejects_spanish_first_track(monkeypatch):
    """A name-`multi` MP4 whose real first track is Spanish is not an Italian or English direct."""
    spa: Stream = {"url": "http://x/spa.mp4"}
    _container_env(
        monkeypatch,
        {
            "http://x/spa.mp4": (
                "mov,mp4,m4a",
                "hevc",
                [Track(1, "spa", "aac"), Track(2, "eng", "aac")],
            ),
        },
        [_R(spa, frozenset({"multi", "spa"}))],
    )
    assert cast_vet.find_instant_direct(_ccfg(), [spa], ("ita", "eng")) is None


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
    cast_playable itself; here we pin that the exact value REACHES it."""
    seen = {}

    def fake_playable(cfg, results, exact_resolution=0):
        seen["exact"] = exact_resolution
        return []

    monkeypatch.setattr(stream_select, "cast_playable", fake_playable)
    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", lambda cfg, s: [])
    cast_vet._reselect_cast_for_lang(_ccfg(), [], {"url": "x"}, "ita", exact_resolution=1080)
    assert seen["exact"] == 1080


def test_vet_cast_audio_threads_exact_to_reselect(monkeypatch):
    seen = {}
    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", lambda cfg, s: [Track(1, "eng", "aac")])
    monkeypatch.setattr(
        cast_vet, "_reselect_cast_for_lang",
        lambda cfg, results, cur, lang, exact_resolution=0, **_kw: seen.update(exact=exact_resolution)
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
        stream_select, "cast_playable",
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


# --- content duration vetting on the cast reselects (ADR 0028) ---------------


def test_reselect_cast_skips_short_candidate(monkeypatch):
    """Twin of the DivX regression above: a placeholder tagged with the right language is
    not a dub. Its lone `und`/tagged track would otherwise win as a 'tagged guess'."""
    chosen: Stream = {"url": "eng-only"}
    fake: Stream = {"url": "fake-ita"}

    def probe(url, **kw):
        if url == "fake-ita":
            return stream_select.tracks.Tracks(
                audio=[Track(1, "ita", "aac")], video_codec="h264", duration=30.0
            )
        return stream_select.tracks.Tracks(
            audio=[Track(1, "eng", "aac")], video_codec="h264", duration=3180.0
        )

    monkeypatch.setattr(stream_select.tracks, "probe_tracks", probe)
    monkeypatch.setattr(cast_vet.availability.tracks, "probe_tracks", probe)
    monkeypatch.setattr(
        stream_select, "cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(fake, frozenset({"ita"}))],
    )  # fmt: skip
    plan = cast_vet.vet_cast_audio(_ccfg(), [chosen, fake], chosen, "ita", expected_s=3300.0)
    assert plan.stream is chosen and plan.mode == "absent"  # fallback + subs, not 30 seconds


def test_reselect_cast_no_extra_probe_for_duration(monkeypatch):
    """The guard must read the memoized probe the audio/video checks already pay for, so
    it costs zero extra ffprobe. Goes through the real `tracks.probe_tracks` (stubbing the
    subprocess, not the memo) — stubbing `probe_tracks` itself would bypass the cache and
    measure nothing."""
    import json as _json

    chosen: Stream = {"url": "eng-only"}
    alt: Stream = {"url": "ita-real"}
    calls: list[str] = []

    class _Proc:
        def __init__(self, url):
            lang = "ita" if url == "ita-real" else "eng"
            self.stdout = _json.dumps(
                {
                    "format": {"duration": "3180.0", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                    "streams": [
                        {"index": 0, "codec_type": "video", "codec_name": "h264"},
                        {
                            "index": 1, "codec_type": "audio", "codec_name": "aac",
                            "tags": {"language": lang},
                        },
                    ],
                }
            )  # fmt: skip

    def run_cmd(cmd, **kw):
        calls.append(cmd[-1])
        return _Proc(cmd[-1])

    monkeypatch.setattr(stream_select.tracks.util, "run_cmd", run_cmd)
    monkeypatch.setattr(
        stream_select, "cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(alt, frozenset({"ita"}))],
    )  # fmt: skip
    stream_select.tracks.clear_cache()
    cast_vet.vet_cast_audio(_ccfg(), [chosen, alt], chosen, "ita")
    without = len(calls)
    calls.clear()
    stream_select.tracks.clear_cache()
    cast_vet.vet_cast_audio(_ccfg(), [chosen, alt], chosen, "ita", expected_s=3300.0)
    assert len(calls) == without  # same ffprobe count with the guard on


# --- ADR 0031 appendix: unresolvable ≠ unprobeable --------------------------


def _dead_swarm(monkeypatch) -> list[str]:
    """Make every infoHash-only stream unresolvable, and record each resolve attempt.

    This is the fixture the suite was missing: before it, every Stream literal in these
    tests carried a `url`, so no test ever exercised the path that crashed in the field."""
    attempts: list[str] = []
    monkeypatch.setattr(stream_select, "_native_resolve", lambda cfg, s: None)

    def boom(cfg, s):
        attempts.append(s["infoHash"])
        raise stream_select.engine.EngineUnavailable("nessun peer")

    monkeypatch.setattr(stream_select.engine, "resolve", boom)
    return attempts


def test_unresolvable_candidate_is_not_a_tagged_guess(monkeypatch):
    """The live crash: an ITA-tagged dead-swarm release probes as `[]` tracks, reads as
    "unprobeable → benefit of the doubt", wins the tagged_guess tier and reaches the
    backends with no url. It must be ineligible instead."""
    _dead_swarm(monkeypatch)
    wrong: Stream = {"url": "rus"}
    dead: Stream = {"infoHash": "deadbeef", "title": "Film.2006.iTA.1080p-GRP"}
    monkeypatch.setattr(
        cast_vet, "_cast_audio_tracks",
        lambda cfg, s: [Track(1, "rus", "aac")] if s is wrong else [],
    )  # fmt: skip
    monkeypatch.setattr(
        stream_select, "cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(dead, frozenset({"ita"}))],
    )  # fmt: skip
    assert cast_vet._reselect_cast_for_lang(_ccfg(), [], wrong, "ita") is None


def test_unresolvable_never_wins_video_reselect(monkeypatch):
    """`_cast_video_codec` returns "" for an unresolvable stream exactly as it does for an
    unprobeable one, so the video gate would wave it through."""
    _dead_swarm(monkeypatch)
    bad: Stream = {"url": "divx"}
    dead: Stream = {"infoHash": "deadbeef", "title": "Film.2006.1080p-GRP"}
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks",
        lambda url: stream_select.tracks.Tracks(video_codec="mpeg4" if url else ""),
    )  # fmt: skip
    monkeypatch.setattr(
        stream_select, "cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(dead, frozenset())],
    )  # fmt: skip
    # Keeps the honest bad-codec verdict → the caller mirrors or fails, never casts a url-less
    # stream. Without the gate this returned `(dead, "")`.
    assert cast_vet.vet_cast_video(_ccfg(), [bad, dead], bad) == (bad, "mpeg4")


def test_unresolvable_never_wins_container_reselect(monkeypatch):
    """With no target language `vet_cast_container`'s `if not target_lang` short-circuits
    past the `plan.verified` guard that protects the language path — so the unresolvable
    candidate could be returned there even though every other gate is language-blind."""
    _dead_swarm(monkeypatch)
    mkv: Stream = {"url": "http://x/film.mkv", "title": "Film.2006.1080p.mkv"}
    dead: Stream = {"infoHash": "deadbeef", "title": "Film.2006.1080p.mp4"}
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks", lambda url: stream_select.tracks.Tracks()
    )
    monkeypatch.setattr(
        stream_select, "cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(dead, frozenset())],
    )  # fmt: skip
    # True = "keep `chosen` and rewrap it to MP4", the honest outcome. Without the gate this
    # returned `(dead, False)` — a direct cast of a stream with no url.
    assert cast_vet.vet_cast_container(_ccfg(), [mkv, dead], mkv, "") == (mkv, True)


def test_unresolvable_consumes_probe_budget(monkeypatch):
    """A dead candidate spends budget like any other probe, so a dead swarm stops early
    instead of walking the whole ranking (ADR 0031 appendix)."""
    _dead_swarm(monkeypatch)
    bad: Stream = {"url": "divx"}
    live: Stream = {"url": "good"}
    dead = [_R({"infoHash": f"h{i}", "title": "F.2006-G"}, frozenset()) for i in range(3)]
    monkeypatch.setattr(
        stream_select.tracks, "probe_tracks",
        lambda url: stream_select.tracks.Tracks(video_codec="mpeg4" if url == "divx" else "h264"),
    )  # fmt: skip
    monkeypatch.setattr(
        stream_select, "cast_playable",
        lambda cfg, results, exact_resolution=0: [*dead, _R(live, frozenset())],
    )  # fmt: skip
    # probe_cap=2 is exhausted by the two dead candidates: the live one is never reached.
    assert cast_vet.vet_cast_video(_ccfg(), [], bad, probe_cap=2) == (bad, "mpeg4")


def test_cast_resolver_walks_past_unresolvable(monkeypatch):
    """An unresolvable best match is not an answer for the language — the resolver used to
    hand back its None and report the dub missing."""
    attempts = _dead_swarm(monkeypatch)
    dead: Stream = {"infoHash": "deadbeef", "title": "Film.2020.iTA.1080p.WEB-DL.AAC-GRP"}
    cfg = Config(torrentio_base="tb", audio_langs=["ita", "eng"])
    monkeypatch.setattr(
        stream_select, "cast_playable",
        lambda c, results, exact_resolution=0: [_R(dead, frozenset({"ita"})), _R(_S_ITA, frozenset({"ita"}))],
    )  # fmt: skip
    assert cast_vet.cast_resolver(cfg, [])("ita") == "http://ita"
    assert attempts == ["deadbeef"]  # the dead one WAS tried, then walked past


def test_reselect_skips_remux_over_the_budget(monkeypatch):
    # Field case 2026-10-01: the only ITA dubs were 66-87GB remuxes with 45GB free. Returning
    # one turned a playable cast (other dub + safety subs) into a refused remux.
    chosen: Stream = {"url": "eng-only"}
    huge: Stream = {"url": "ita-66gb"}

    def tracks_of(cfg, s):
        return (
            [Track(1, "eng", "aac")]
            if s is chosen
            else [Track(1, "eng", "dts"), Track(2, "ita", "dts")]
        )

    monkeypatch.setattr(cast_vet, "_cast_audio_tracks", tracks_of)
    monkeypatch.setattr(cast_vet.quality, "remux_size_budget", lambda cfg: 20)
    monkeypatch.setattr(
        stream_select, "cast_playable",
        lambda cfg, results, exact_resolution=0: [_R(huge, frozenset({"ita"}), size_gb=66.6)],
    )  # fmt: skip
    plan = cast_vet.vet_cast_audio(_ccfg(), [chosen, huge], chosen, "ita")
    assert plan.stream is chosen and plan.mode == "absent"


def test_instant_direct_considers_untagged_mp4(monkeypatch):
    # The most common direct-castable release: a plain English WEB-DL .mp4 with no language
    # tag in its name. The tag-only filter never probed it.
    mkv: Stream = {"url": "http://x/ita.mkv"}
    plain: Stream = {"url": "http://x/plain.mp4"}
    _container_env(
        monkeypatch,
        {
            "http://x/ita.mkv": ("matroska,webm", "hevc", [Track(1, "ita", "dts", 6)]),
            "http://x/plain.mp4": ("mov,mp4,m4a", "h264", [Track(1, "eng", "aac")]),
        },
        [_R(mkv, frozenset({"ita"})), _R(plain, frozenset(), container="mp4")],
    )
    plan = cast_vet.find_instant_direct(_ccfg(), [mkv, plain], ("ita", "eng"))
    assert plan is not None and plan.stream is plain and plan.real_lang == "eng"
