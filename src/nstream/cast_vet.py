"""Cast-path stream vetting: audio plan, video codec, container (ADR 0017/0022).

Owns cast-stack policy (cast_flow → cast_vet → backends). `stream_select` keeps
prepare_stream / ranking / URL resolution and exposes helpers this module uses
(`playable_url`, `cast_playable`, `audio_languages`).

Invariant every candidate loop here upholds: **a candidate that cannot be resolved to a url
is ineligible, not unprobeable.** Each loop resolves once (after the probe-cap break, so an
over-cap candidate never costs a P2P buffering wait) and skips on failure, so the `""` codec,
`[]` tracks and unknown duration the gates below grant the benefit of the doubt to can only
ever mean *resolved but unprobeable* — the case ADR 0017/0022/0028 actually meant. Without it
a dead-swarm candidate sails through every gate and reaches the backends with no `url`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import availability, languages, log, notices, quality, remux, stream_select, tracks
from .config import Config
from .types import Stream


def cast_languages(
    cfg: Config, results: list[Stream], *, exact_resolution: int = 0, title: str = ""
) -> tuple[str, ...]:
    """Audio languages available among Cast-compatible streams, preferred ones first."""
    return stream_select.audio_languages(
        cfg, results, cast=True, exact_resolution=exact_resolution, title=title
    )


def cast_resolver(
    cfg: Config, results: list[Stream], *, exact_resolution: int = 0, title: str = ""
) -> Callable[[str], str | None]:
    """Return a fn picking the best Cast-compatible stream URL for a language, or None.
    Closes over the already-fetched `results` so switching needs no extra network call."""
    playable = stream_select.cast_playable(
        cfg, results, exact_resolution=exact_resolution, title=title
    )

    def resolve(lang: str) -> str | None:
        for r in playable:  # already ranked best-first
            if lang in r.info.languages:
                # An unresolvable best match is not an answer for the language: keep walking
                # instead of reporting the dub missing (ADR 0031 appendix).
                url = stream_select.playable_url(cfg, r.stream)
                if url:
                    return url
        return None

    return resolve


@dataclass(frozen=True)
class CastAudioPlan:
    """How to cast a stream so the audio plays in the target language. The Chromecast Default
    Media Receiver plays a file's FIRST audio track and can't switch tracks in place, so the
    language is decided here (selection time), not on the device:
      - `direct`: the first track is already the target language + DMR-decodable → cast the url.
      - `remux`:  the target language is present but not the first decodable track → remux
                  keeping only `audio_index` (a single-track AAC/copy file the DMR plays right).
      - `absent`: no candidate carries the target language → caller falls back (other dub + subs).
    `real_lang` is the language that will actually play (for honest `audio_lang` reporting).

    `needs_remux` is orthogonal to `mode`: it flags that the track we'll actually cast
    (`audio_index`) has a codec the DMR can't decode (AC-3/E-AC-3/DTS/…), so it must be
    remuxed to AAC regardless of the language decision. `mode == "remux"` already implies it;
    the field carries the same requirement into the `absent` fallback, where we still cast a
    (wrong-language) dub and it would otherwise go out silent as a direct cast."""

    mode: str
    stream: Stream
    audio_index: int = 0  # audio-relative index of the chosen track → ffmpeg `-map 0:a:<i>`
    real_lang: str | None = None
    verified: bool = False  # True when decided from real ffprobe tracks (not a name guess)
    needs_remux: bool = False  # cast track's codec is undecodable → Tier-2 remux even if `absent`


def _cast_audio_tracks(cfg: Config, stream: Stream) -> list[tracks.Track]:
    """Probed audio tracks of `stream` (url resolved first), or [] when resolved but
    unprobeable — the loops above gate out unresolvable candidates first."""
    url = stream_select.playable_url(cfg, stream)
    return list(tracks.probe_tracks(url).audio) if url else []


def _cast_video_codec(cfg: Config, stream: Stream) -> str:
    """REAL video codec of `stream` per ffprobe, "" when unprobeable. Memoized with the
    audio probe (same url, same call) — the video vetting costs no extra network read."""
    url = stream_select.playable_url(cfg, stream)
    return tracks.probe_tracks(url).video_codec if url else ""


def _video_castable(cfg: Config, stream: Stream) -> bool:
    """Whether the DMR can render `stream`'s probed video. Unknown ("" — resolved but no
    ffprobe / probe failed) keeps the benefit of the doubt, mirroring the audio vetting's
    stance. An UNRESOLVABLE stream is not "unknown": callers gate it out beforehand."""
    codec = _cast_video_codec(cfg, stream)
    return not codec or codec in quality.CAST_VIDEO_DECODABLE


def _duration_castable(cfg: Config, stream: Stream, expected_s: float) -> bool:
    """Whether `stream` really is the video and not a placeholder/sample (ADR 0028). Same
    stance as `_video_castable`: unknown keeps the benefit of the doubt. Reads the memoized
    probe the audio/video checks around it already pay — no extra network read."""
    if expected_s <= 0:
        return True
    url = stream_select.playable_url(cfg, stream)
    return availability.vet_duration(url or "", expected_s).ok


@log.phase("vet_video")
def vet_cast_video(
    cfg: Config,
    results: list[Stream],
    chosen: Stream,
    *,
    probe_cap: int = 4,
    exact_resolution: int = 0,
    title: str = "",
    expected_s: float = 0.0,
) -> tuple[Stream, str]:
    """Verify the DMR can render `chosen`'s REAL video before casting (ADR 0017). The name
    parse gives an untagged release the benefit of the doubt, but an MPEG-4 ASP/DivX rip
    casts as PLAYING + black screen with no receiver error — selection time is the only
    place this class of failure can be caught.

    Returns `(stream, "")` when `chosen` (or a reselected candidate) is castable, or
    `(chosen, bad_codec)` when nothing qualifies — the caller falls back to the mirror
    (mpv decodes locally) or fails explicitly instead of casting black. Reselection walks
    the ranked cast-playable candidates, probing at most `probe_cap`."""
    bad = _cast_video_codec(cfg, chosen)
    # The probe above resolves as a side effect, so a still-missing url means `chosen` is
    # UNRESOLVABLE — not the "unprobeable → benefit of the doubt" case (ADR 0031 appendix).
    # Reselect rather than declaring it castable, or a live alternative never gets its turn.
    if chosen.get("url") and (not bad or bad in quality.CAST_VIDEO_DECODABLE):
        return chosen, ""
    why = f"video {bad.upper()} non decodificabile dal TV" if bad else "sorgente non risolvibile"
    probed = 0
    for r in stream_select.cast_playable(
        cfg, results, exact_resolution=exact_resolution, title=title
    ):
        s = r.stream
        if s is chosen or s.get("url") == chosen.get("url"):
            continue
        if probed >= probe_cap:
            break
        probed += 1
        # Resolved AFTER the cap break and the budget spend: resolving an over-cap candidate
        # would cost a P2P buffering wait for a stream we'd discard (stream_select doctrine),
        # and an unresolvable one must consume budget like any other probe.
        if not stream_select.playable_url(cfg, s):
            continue
        if _video_castable(cfg, s) and _duration_castable(cfg, s, expected_s):
            notices.emit(f"{why} → altra release")
            return s, ""
    return chosen, bad


def cast_container(cfg: Config, stream: Stream) -> str:
    """Canonical container of `stream` for cast compatibility (ADR 0022). The filename
    extension (name-parsed) is authoritative — it is the only signal that splits the shared
    matroska/webm ffprobe demuxer name — and the ffprobe `format_name` (same memoized probe
    as the audio/video vetting) confirms or overrides it toward INCOMPATIBLE, so a `.mp4`
    that is really Matroska is rewrapped, not cast black. "" = unknown (benefit of the doubt).

    Public (ADR 0022): `cast_flow` reads it for the settled-stream rewrap verdict and the
    LOAD contentType, so it must not be a leading-underscore reach-through."""
    ext = quality.parse_stream(stream).container
    url = stream_select.playable_url(cfg, stream)
    probed = quality.container_from_format(tracks.probe_tracks(url).container, ext) if url else ""
    return probed or ext


def _container_castable(cfg: Config, stream: Stream) -> bool:
    """Whether the DMR can LOAD `stream`'s container on a direct cast (ADR 0022)."""
    return quality.container_castable(cast_container(cfg, stream))


@log.phase("vet_container")
def vet_cast_container(
    cfg: Config,
    results: list[Stream],
    chosen: Stream,
    target_lang: str,
    *,
    probe_cap: int = 4,
    exact_resolution: int = 0,
    title: str = "",
    expected_s: float = 0.0,
) -> tuple[Stream, bool]:
    """Ensure the DMR can LOAD `chosen`'s container before a direct cast (ADR 0022). The
    Default Media Receiver refuses Matroska (.mkv) — player UNKNOWN + receiver ERROR,
    content_id None — yet plays the SAME HEVC/AAC in MP4. Prefer a swap over a download, but
    only to a **strict win**: a *verified direct cast in `target_lang`* with a DMR-compatible
    container and castable video. A merely name-`multi`-tagged MP4 whose REAL first audio track
    is another dub must NOT preempt the target-language rewrap — trusting the name tag here is
    how an Italian request landed on a Spanish MP4. If nothing qualifies, keep `chosen` and
    return True so the caller rewraps to MP4 while `vet_cast_audio` still selects the target dub.

    Returns `(stream, needs_container_rewrap)`."""
    if _container_castable(cfg, chosen):
        return chosen, False
    probed = 0
    for r in stream_select.cast_playable(
        cfg, results, exact_resolution=exact_resolution, title=title
    ):
        s = r.stream
        if s is chosen or s.get("url") == chosen.get("url"):
            continue
        langs = r.info.languages
        if target_lang and target_lang not in langs and "multi" not in langs:
            continue
        if probed >= probe_cap:
            break
        probed += 1
        # Eligibility, not castability: an unresolvable candidate can't be probed and can't be
        # cast, and every gate below would wave it through on the benefit of the doubt.
        if not stream_select.playable_url(cfg, s):
            continue
        if not (
            _container_castable(cfg, s)
            and _video_castable(cfg, s)
            and _duration_castable(cfg, s, expected_s)
        ):
            continue
        # Language-safe swap: only a probed, verified direct cast in the target language beats
        # the target-language rewrap of `chosen`. Same memoized probe as the checks above.
        plan = _cast_plan_for(s, _cast_audio_tracks(cfg, s), target_lang)
        if not target_lang or (
            plan.mode == "direct" and plan.verified and plan.real_lang == target_lang
        ):
            notices.emit(
                "container non caricabile dal TV → altra release MP4 (stessa lingua)",
            )
            return s, False
    return chosen, True


def _cast_plan_for(stream: Stream, audio: list[tracks.Track], target_lang: str) -> CastAudioPlan:
    """Decide the cast plan for one resolved `stream` given its probed `audio` tracks and the
    desired `target_lang` (a canonical code, or "" for no preference → codec-only legacy
    behaviour). The DMR plays `audio[0]`, so a direct cast is correct only when that track is
    the target language and decodable; otherwise, if a target-language track exists anywhere,
    remux selects it."""
    if not audio:  # unprobeable (no ffprobe / no tracks) → benefit of the doubt, cast directly
        return CastAudioPlan("direct", stream, 0, target_lang or None, verified=False)
    codes = [languages.track_lang(t.lang, t.title) for t in audio]
    c0 = audio[0].codec.lower()
    if not target_lang:  # no language preference: codec-only decision on the default track
        mode = "remux" if remux.needs_remux(c0) else "direct"
        return CastAudioPlan(mode, stream, 0, codes[0], verified=True)
    if not any(codes):
        # Every track's language is unknown (und/untagged): mirror the local guard's benefit
        # of the doubt instead of declaring the dub absent — a single ita track tagged `und`
        # was already right, and "absent" would force wrong subs + report the wrong lang.
        # The codec still decides direct vs remux.
        mode = "remux" if remux.needs_remux(c0) else "direct"
        return CastAudioPlan(mode, stream, 0, target_lang or None, verified=False)
    if codes[0] == target_lang and remux.dmr_decodable(c0):
        return CastAudioPlan("direct", stream, 0, target_lang, verified=True)
    k = next((i for i, c in enumerate(codes) if c == target_lang), None)
    if k is not None:
        return CastAudioPlan("remux", stream, k, target_lang, verified=True)
    # Target language genuinely absent: the caller casts this dub anyway (+ safety subs). Its
    # default track still has to be DECODABLE — a Dolby/DTS first track would go out silent on
    # a direct cast — so flag a remux of track 0, orthogonally to the language being absent.
    return CastAudioPlan(
        "absent", stream, 0, codes[0], verified=True, needs_remux=not remux.dmr_decodable(c0)
    )


def _reselect_cast_for_lang(
    cfg: Config,
    results: list[Stream],
    current: Stream,
    target_lang: str,
    *,
    probe_cap: int = 6,
    exact_resolution: int = 0,
    title: str = "",
    expected_s: float = 0.0,
) -> CastAudioPlan | None:
    """Find another cast candidate (best-first) carrying `target_lang`, preferring one castable
    directly (target is the first decodable track) over one needing a remux. Probes up to
    `probe_cap` candidates whose NAME claims the language (tagged `target_lang` or `multi`) —
    spending the probe budget on releases that actually advertise it, rather than on untagged
    ones that usually don't. None if none qualifies.

    Preference order: a *verified* direct cast (probed, target is the decodable first track) >
    a *verified* remux (target present, not first) > a name-tagged-target release whose tracks
    were unprobeable (benefit of the doubt). The last tier matters because non-cached releases
    routinely can't be ffprobed cheaply, so a title's only `target_lang` dubs may all come back
    unverified — and a release literally tagged `ITA` is a far better bet than falling back to
    the wrong-language pick the caller would otherwise cast. This mirrors the benefit of the
    doubt `pick_audio_stream_verified` already gives the forced `--audio-lang` path."""
    remux_fallback: CastAudioPlan | None = None
    tagged_guess: CastAudioPlan | None = None
    direct_bad_container: CastAudioPlan | None = None
    budget = quality.remux_size_budget(cfg)
    probed = 0
    for r in stream_select.cast_playable(
        cfg, results, exact_resolution=exact_resolution, title=title
    ):
        s = r.stream
        if s is current or s.get("url") == current.get("url"):
            continue
        langs = r.info.languages
        if target_lang not in langs and "multi" not in langs:  # only chase claimed-language dubs
            continue
        if probed >= probe_cap:
            break
        probed += 1
        # Unresolvable → ineligible, before any gate. Otherwise its `[]` tracks read as
        # "unprobeable", `_cast_plan_for` returns an unverified direct plan, and it wins the
        # `tagged_guess` tier below — the candidate behind the KeyError: 'url' crash.
        if not stream_select.playable_url(cfg, s):
            continue
        # A dub with video the DMR can't render is not a candidate: reselecting it trades
        # silent-wrong-language for a black screen (the live failure behind ADR 0017 —
        # a cached ITA DivX rip won this loop). Same probe as the audio read below (memoized).
        if not _video_castable(cfg, s):
            continue
        # A placeholder/sample with the right language tag is not a dub (ADR 0028): its lone
        # `und` track would sail through as an unverified "tagged guess" below.
        if not _duration_castable(cfg, s, expected_s):
            continue
        plan = _cast_plan_for(s, _cast_audio_tracks(cfg, s), target_lang)
        if plan.mode == "direct" and plan.verified:
            # A direct dub in an MKV would fail the DMR LOAD (ADR 0022): keep chasing a
            # DMR-compatible container (a free direct cast), remembering the MKV as a fallback
            # the caller rewraps to MP4. Same memoized probe as the audio read above.
            if _container_castable(cfg, s):
                return plan  # cheapest *verified* correct option (no download) → take it
            if direct_bad_container is None:
                direct_bad_container = plan
        elif plan.mode == "remux" and remux_fallback is None:
            # Remember, but keep looking for a direct one. A remux over the size/disk budget
            # is not an option at all: the cast-time guard refuses it, and preferring it over
            # the original pick turned a playable cast (other dub + safety subs) into a
            # failure (2026-10-01: the only ITA remuxes were 66-87GB with 45GB free).
            if not (budget and r.info.size_gb > budget):
                remux_fallback = plan
        elif (
            plan.mode == "direct"
            and not plan.verified
            and target_lang in langs  # the NAME explicitly claims it (not just "multi")
            and tagged_guess is None
        ):
            # Unprobeable but explicitly target-tagged → a benefit-of-the-doubt last resort,
            # kept only if no verified option turns up (below any remux_fallback).
            tagged_guess = plan
    return remux_fallback or direct_bad_container or tagged_guess


@log.phase("vet_instant")
def find_instant_direct(
    cfg: Config,
    results: list[Stream],
    langs: tuple[str, ...],
    *,
    exact_resolution: int = 0,
    title: str = "",
    expected_s: float = 0.0,
    probe_cap: int = 6,
) -> CastAudioPlan | None:
    """Verified direct cast in the earliest `langs` entry that has one (ADR 0035).

    A direct cast is MP4/WebM, a DMR-decodable first audio track, and that track's
    language confirmed by ffprobe. Name tags only decide who is worth a probe; an
    unprobeable release and a `multi` whose real first track is another language do
    not qualify. The probe budget matches `_reselect_cast_for_lang`."""
    wanted = [lang for lang in langs if lang]
    if not wanted:
        return None
    wanted_set = set(wanted)
    best: CastAudioPlan | None = None
    best_rank = len(wanted)
    probed = 0
    for r in stream_select.cast_playable(
        cfg, results, exact_resolution=exact_resolution, title=title
    ):
        named = r.info.languages
        # Worth a probe: a release whose name claims a wanted language, or an UNTAGGED one
        # already in a DMR-loadable container — the most common direct-castable kind (a
        # plain English WEB-DL .mp4), which the tag-only filter never considered.
        tagged = bool(named & wanted_set or "multi" in named)
        untagged_direct = not named and r.info.container in quality.CAST_CONTAINER_DECODABLE
        if not (tagged or untagged_direct):
            continue
        if probed >= probe_cap:
            break
        probed += 1
        s = r.stream
        if not stream_select.playable_url(cfg, s):
            continue
        if not (_video_castable(cfg, s) and _duration_castable(cfg, s, expected_s)):
            continue
        if cast_container(cfg, s) not in quality.CAST_CONTAINER_DECODABLE:
            continue
        audio = _cast_audio_tracks(cfg, s)
        if not audio:
            continue
        code = languages.track_lang(audio[0].lang, audio[0].title)
        if code not in wanted_set or not remux.dmr_decodable(audio[0].codec):
            continue
        rank = wanted.index(code)
        if rank < best_rank:
            best = CastAudioPlan("direct", s, 0, code, verified=True)
            best_rank = rank
            if rank == 0:
                return best
    return best


def instant_defer_notice(slow: CastAudioPlan, instant: CastAudioPlan, target: str) -> str:
    """Why headless started `instant` instead of waiting for `slow` (ADR 0035)."""
    info = quality.parse_stream(slow.stream)
    detail = [info.container or "?", info.audio or "?"]
    if info.size_gb:
        detail.append(f"~{info.size_gb:.1f}GB")
    played = instant.real_lang or "?"
    return f"audio {target} solo dopo remux completo ({', '.join(detail)}); cast diretto {played}"


@log.phase("vet_audio")
def vet_cast_audio(
    cfg: Config,
    results: list[Stream],
    chosen: Stream,
    target_lang: str,
    *,
    exact_resolution: int = 0,
    title: str = "",
    expected_s: float = 0.0,
) -> CastAudioPlan:
    """Decide how to cast `chosen` so the audio plays in `target_lang` (default `cfg.primary`;
    "" = no preference). Probes the real tracks — the DMR plays the first one and can't switch —
    and, when `chosen` carries no `target_lang` track, reselects a candidate that does. Returns
    a CastAudioPlan; `absent` means no candidate has the language (caller does fallback+subs)."""
    plan = _cast_plan_for(chosen, _cast_audio_tracks(cfg, chosen), target_lang)
    if not target_lang:
        return plan
    # Confident plans for the best pick win outright: a verified direct cast, or a remux that
    # selects the target track. Otherwise (probe failed → unverified guess, or the language is
    # absent from this release) search the other dubs for a *verified* one before falling back.
    if (plan.mode == "direct" and plan.verified) or plan.mode == "remux":
        return plan
    return (
        _reselect_cast_for_lang(
            cfg,
            results,
            chosen,
            target_lang,
            exact_resolution=exact_resolution,
            title=title,
            expected_s=expected_s,
        )  # fmt: skip
        or plan
    )
