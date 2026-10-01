"""Stream selection, resolution, and the auto-play vetting guards.

Owns ranking, quality UX, URL resolve, and the primary-language audio guard.
`prepare_stream` is the single entry the orchestrator calls.

Availability probes / dead-source denylist: `availability`.
Cast-path vetting: `cast_vet`."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import (
    addons,
    api,
    availability,
    debrid,
    engine,
    languages,
    log,
    notices,
    quality,
    sources,
    tracks,
    ui,
)
from .config import Config, PlayOpts
from .types import Stream

_log = log.get_logger("stream_select")


class ContentTooShort(Exception):
    """Every probed source is a placeholder/sample, not the video (ADR 0028). Mirrors
    `cast_flow.CastVideoUnsupported`: the callers surface it (headless: `sources_truncated`)
    instead of playing 30 seconds of "removed for copyright" and reporting success."""

    def __init__(self, verdict: availability.DurationVerdict, count: int = 1) -> None:
        super().__init__(verdict.reason or "sorgente troncata")
        self.verdict = verdict
        self.count = count


class AudioLangUnavailable(Exception):
    """Forced `--audio-lang` is not present on any (remaining) playable stream.

    Shared by TUI and headless so both paths fail the same way — never silently play
    another dub when the user named a language."""

    def __init__(self, lang: str, available: tuple[str, ...], *, real_tracks: bool = False) -> None:
        where = "tracce reali" if real_tracks else "sorgenti"
        super().__init__(f"audio «{lang}» non disponibile nelle {where}")
        self.lang = lang
        self.available = available
        self.real_tracks = real_tracks


class QualityUnavailable(Exception):
    """Hard quality filter (CLI `--quality` or `cfg.default_quality`) left no playable stream.

    Shared by TUI and headless: never silently fail with an empty pick."""

    def __init__(self, quality: int, available: list[int]) -> None:
        super().__init__(f"nessuno stream {quality}p riproducibile")
        self.quality = quality
        self.available = list(available)


class NoPlayableStream(Exception):
    """The title has streams, but none of them can be served right now.

    Exists to split the two meanings `None` used to carry on the selection path: `None` is
    the user backing out (ESC), this is exhaustion. Only the latter deserves a notice, and a
    notice that survives the fzf redraw — see `cli._play_video`, which turns `reason` into
    the menu header."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def unresolvable_reason(
    cfg: Config, results: list[Stream], chosen: Stream | None = None
) -> str | None:
    """Why nothing can be turned into a playable url, or None when the reason lies elsewhere
    (hardware/cast filters, quality tier) and the caller has a better message.

    `chosen` — the candidate that just failed to resolve — is consulted first: a pure torrent
    refused by the privacy gate is the exact answer, even when other rows in `results` still
    carry a url the ranking put behind it. Without it, the result set's *shape* answers.

    Provider-agnostic by construction: it reports what the streams look like (no direct link,
    only torrents) and never names — nor probes — a debrid service."""
    blocked = engine.p2p_block_reason(cfg)
    if chosen is not None and blocked and chosen.get("infoHash") and not chosen.get("url"):
        return f"{blocked} — la sorgente scelta è un torrent, attiva la VPN o usa un debrid"
    if not results or any(s.get("url") and not s.get("unresolvable") for s in results):
        return None
    if blocked and any(s.get("infoHash") for s in results):
        return f"solo sorgenti torrent e {blocked} — attiva la VPN o usa un debrid"
    if debrid.get_resolver(cfg) is None:
        return "nessun link diretto dalle fonti debrid — token scaduto o titolo non in cache"
    return None


def _future_release(iso: str | None) -> datetime | None:
    """Parse a Cinemeta `released` ISO date; return it only if it's in the future."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt > datetime.now(UTC) else None


def no_streams_message(cfg: Config, typ: str, video_id: str, title: str) -> str:
    """A specific notice when a title has no streams (config / unreleased / empty)."""
    if not addons.has_stream_source(cfg):
        return sources.no_stream_source_message()
    released = _future_release(api.meta(cfg, typ, video_id).get("released"))
    if released:
        return (
            f"{ui.g().movie} «{title}» non ancora disponibile — "
            f"uscita prevista il {released:%d/%m/%Y}"
        )
    return f"nessuno stream disponibile per «{title}»"


@dataclass(frozen=True)
class StreamMenu:
    """What the manual stream menu shows (ADR 0037): the domain ranks and curates, the
    frontend (`menus.choose_stream`) labels, caps and prompts. `cap` = how many playable
    rows to show before a "show all" entry (0 = all); `notice` = the header line."""

    playable: list[quality.RankedStream]
    excluded: list[quality.RankedStream]
    notice: str | None = None
    cap: int = 0


ChooseStream = Callable[[StreamMenu], "Stream | None"]


def no_stream_source_error(cfg: Config) -> str | None:
    """JSON/headless error code when no stream addon is configured, else None."""
    if addons.has_stream_source(cfg):
        return None
    return "no_stream_sources"


def exact_resolution(quality_choice: int | None) -> int:
    """Map a PlayOpts.quality value to FilterSpec.exact_resolution (0 = no filter)."""
    return quality_choice if quality_choice and quality_choice > 0 else 0


def available_resolutions(cfg: Config, results: list[Stream], *, cast: bool = False) -> list[int]:
    """Resolutions present among playable streams (HW/cast filters, no quality filter)."""
    return quality.resolutions_of(_playable_set(cfg, results, cast=cast))


def pick_quality(
    cfg: Config,
    results: list[Stream],
    choose: Callable[..., Any],
    *,
    cast: bool = False,
    title: str = "",
) -> int | None:
    """In-flow quality picker: Auto + resolutions present for this title, asked through the
    frontend's `choose` (ADR 0037). Returns 0 (Auto), N (exact res), or None on ESC. Only
    offers tiers that exist among playable streams."""
    res_list = available_resolutions(cfg, results, cast=cast)
    items: list[tuple[str, int]] = [(quality.quality_label(0), 0)]
    items += [(quality.quality_label(r), r) for r in res_list]
    header = f"qualità · {title}" if title else "qualità"
    return choose(items, "qualità> ", header=header)


def resolve_quality(
    cfg: Config,
    results: list[Stream],
    opts: PlayOpts,
    *,
    cast: bool,
    title: str = "",
    offer_picker: bool,
) -> int | None:
    """Decide the session quality: CLI/opts win; else config default; else picker; else Auto.

    Returns None only when the user ESC'd the picker. `cfg.default_quality` is None (ask
    when interactive), 0 (Auto without asking), or N (exact tier). Applied on **every**
    path — TUI, headless, binge unattended — so a settings default is not silently dropped
    when `offer_picker` is False (ADR 0021)."""
    if opts.quality is not None:
        return opts.quality
    if cfg.default_quality is not None:
        return cfg.default_quality
    if not offer_picker or opts.choose is None:
        return 0  # headless / binge without sticky/default → Auto
    return pick_quality(cfg, results, opts.choose, cast=cast, title=title)


def _pick_stream(
    cfg: Config,
    results: list[Stream],
    *,
    auto: bool,
    cast: bool = False,
    title: str = "",
    exact_resolution: int = 0,
    menu: ChooseStream | None = None,
) -> Stream | None:
    """Rank and curate streams, then auto-pick the best or hand a `StreamMenu` to the
    frontend's `menu` (ADR 0037: top N playable + a 'show all' entry that reveals the rest
    and the excluded ones ⚠). Without a `menu` the manual path auto-picks.

    When `cast`, rank against the Chromecast receiver's profile (not the laptop GPU)
    and demote streams whose audio it can't decode (TrueHD/DTS/DTS-HD → silent).
    `exact_resolution` (>0) hard-filters to that resolution before ranking.

    Returns None only when the user backed out of the menu; an empty ranking raises
    `NoPlayableStream` — except under `exact_resolution`, which the caller reports as
    `QualityUnavailable` (it knows which tiers do exist)."""
    if not cfg.hw_filter:
        pool = results
        if exact_resolution:
            pool = [s for s in results if quality.parse_stream(s).resolution == exact_resolution]
        if not pool:
            if exact_resolution:  # exhaustion, not ESC (ADR 0033)
                tiers = sorted({quality.parse_stream(s).resolution for s in results} - {0})
                raise QualityUnavailable(exact_resolution, tiers)
            raise NoPlayableStream("nessuno stream disponibile")
        if auto or menu is None:
            return pool[0]
        return menu(
            StreamMenu([quality.RankedStream(s, quality.parse_stream(s)) for s in pool], [])
        )

    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(
        cfg, cast_audio=cast, title=title, exact_resolution=exact_resolution
    )
    playable, excluded = quality.rank_streams(results, caps, spec)
    # Build a notice that survives into the fzf header (stderr scrolls away under fullscreen).
    notice_parts: list[str] = []
    if exact_resolution:
        notice_parts.append(f"qualità {exact_resolution}p")
    if excluded:
        reasons = ", ".join(sorted({r.reason for r in excluded if r.reason}))
        notice_parts.append(f"{len(excluded)} stream filtrati ({reasons})")
    dupes = len(results) - len(playable) - len(excluded)
    if dupes > 0:
        notice_parts.append(f"{dupes} doppioni rimossi")
    notice = "  ·  ".join(notice_parts) if notice_parts else None
    if auto or menu is None:
        # No menu → ranking summary as a secondary status line (not a hard `nstream:` error).
        if notice:
            ui.status_detail(notice)
        if playable:
            return playable[0].stream
        if exact_resolution:  # exhaustion, not ESC (ADR 0033)
            raise QualityUnavailable(
                exact_resolution, available_resolutions(cfg, results, cast=cast)
            )
        raise NoPlayableStream(
            "nessuno stream compatibile col Chromecast (prova Tab o --local)"
            if cast
            else "nessuno stream supportato dall'hardware"
        )

    if not playable and not excluded:
        # Empty menu: fzf wouldn't even open, and a bare return is indistinguishable from ESC.
        raise NoPlayableStream(
            f"nessuno stream {exact_resolution}p"
            if exact_resolution
            else (unresolvable_reason(cfg, results) or "nessuno stream disponibile")
        )

    return menu(StreamMenu(playable, excluded, notice, cfg.max_streams))


def _playable_set(
    cfg: Config,
    results: list[Stream],
    *,
    cast: bool,
    exact_resolution: int = 0,
) -> list[quality.RankedStream]:
    """Streams playable on the target profile (Chromecast or local GPU), ranked best-first,
    ignoring the language filter so every available dub is visible (for listing/switching).
    Optional `exact_resolution` applies the per-session quality hard-filter."""
    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(
        cfg, cast_audio=cast, lang_filter=False, exact_resolution=exact_resolution
    )
    playable, _ = quality.rank_streams(results, caps, spec)
    return playable


def cast_playable(
    cfg: Config, results: list[Stream], *, exact_resolution: int = 0
) -> list[quality.RankedStream]:
    """Streams the Chromecast can play (cast profile + Cast-compatible audio).

    `exact_resolution` is the resolved per-invocation quality choice (ADR 0021): EVERY
    cast (re)selection path must thread it, or a reselect can legally return a release
    the user's `--quality` excluded — the parity defect that bit three times in one day."""
    return _playable_set(cfg, results, cast=True, exact_resolution=exact_resolution)


def audio_languages(
    cfg: Config, results: list[Stream], *, cast: bool, exact_resolution: int = 0
) -> tuple[str, ...]:
    """Audio languages available among playable streams (local or cast profile), with the
    user's preferred languages first. Name-tag based, like the rest of the language ranking."""
    langs = {
        lang
        for r in _playable_set(cfg, results, cast=cast, exact_resolution=exact_resolution)
        for lang in r.info.languages
        if lang != "multi"
    }
    ordered = [lang for lang in cfg.audio_langs if lang in langs]
    ordered += sorted(langs - set(ordered))
    return tuple(ordered)


def pick_audio_stream(
    cfg: Config,
    results: list[Stream],
    lang: str,
    *,
    cast: bool,
    exact_resolution: int = 0,
) -> Stream | None:
    """The best playable stream whose audio includes `lang` (url resolved), or None."""
    for r in _playable_set(cfg, results, cast=cast, exact_resolution=exact_resolution):
        if lang in r.info.languages and playable_url(cfg, r.stream):
            return r.stream
    return None


def pick_audio_stream_verified(
    cfg: Config,
    results: list[Stream],
    lang: str,
    *,
    cast: bool,
    probe_cap: int = 4,
    exact_resolution: int = 0,
    expected_runtime_s: float = 0.0,
) -> tuple[Stream | None, bool]:
    """Track-accurate variant: among the playable streams whose NAME tags `lang`, ffprobe up
    to `probe_cap` candidates and return the first whose REAL audio tracks carry `lang`
    (verified=True). When a candidate's real tracks are unverifiable (und/no ffprobe), accept
    it on benefit of the doubt (verified=False). Returns (None, False) only when every
    name-match's real tracks are known AND lack `lang` — i.e. the name lied for all of them.

    A candidate whose real duration is a fraction of `expected_runtime_s` is dropped before
    the audio check (ADR 0028) — it is a placeholder, and its (often `und`) track would
    otherwise be accepted on benefit of the doubt. Costs no extra ffprobe: `probe_tracks`
    runs on the same url a line later. Raises `ContentTooShort` when every name-match is
    truncated, so the caller never reports it as `audio_lang_unavailable`."""
    name_pick: Stream | None = None
    probed = 0
    short: list[Stream] = []
    last_short: availability.DurationVerdict | None = None
    for r in _playable_set(cfg, results, cast=cast, exact_resolution=exact_resolution):
        if lang not in r.info.languages:
            continue
        if probed >= probe_cap:
            # Cap checked BEFORE resolving: `playable_url` on an over-cap candidate can
            # cost a P2P buffering wait (or a debrid add) for a stream we'd discard anyway.
            break
        url = playable_url(cfg, r.stream)
        if not url:
            continue
        probed += 1
        verdict = availability.vet_duration(url, expected_runtime_s)
        if not verdict.ok:
            _log.info("scarto sorgente troncata: %s", verdict.reason)
            short.append(r.stream)
            last_short = verdict
            continue
        if name_pick is None:
            name_pick = r.stream
        real = stream_audio_langs(cfg, r.stream)
        if real is None:
            return r.stream, False  # unverifiable → benefit of the doubt
        if lang in real:
            return r.stream, True  # confirmed by the actual tracks
    if short:
        # Bind the verdict to the run: no later reselect can land back on a proven placeholder.
        availability.drop_streams(results, short)
        if name_pick is None and last_short is not None:
            raise ContentTooShort(last_short, len(short))
    # Every probed name-match had real tracks WITHOUT `lang` (name mistagged) → no match.
    return (None, False) if name_pick is not None and probed else (name_pick, False)


def _native_resolve(cfg: Config, stream: Stream) -> str | None:
    """Resolve a pure-torrent stream through the configured native debrid API, or None
    (best-effort: not the native backend, no resolver for the provider, or a provider error
    — the caller then falls back to local P2P). Never raises."""
    if cfg.playback_backend != "native":
        return None
    resolver = debrid.get_resolver(cfg)
    if resolver is None:
        return None
    try:
        return resolver.resolve(stream)
    except debrid.DebridUnavailable as e:
        _log.info("native resolve fallita: %s", e)
        return None


def playable_url(cfg: Config, stream: Stream) -> str | None:
    """Ready url for a stream, resolving a pure-torrent (infoHash) one through the native
    debrid API (native backend) or the P2P engine on demand. Best-effort and silent: returns
    None if neither can serve it (the caller already has a user-facing fallback).

    Memoized both ways on the stream dict: a success caches the `url`, a failure stamps
    `unresolvable`. Without the negative half a dead swarm pays `engine._wait_buffer`'s full
    timeout again on every gate that probes the same candidate (ADR 0031 appendix)."""
    if stream.get("url"):
        return stream["url"]
    if stream.get("unresolvable") or not stream.get("infoHash"):
        return None
    native = _native_resolve(cfg, stream)
    if native:
        stream["url"] = native
        return native
    try:
        stream["url"] = engine.resolve(cfg, stream)
        return stream["url"]
    except engine.EngineUnavailable:
        stream["unresolvable"] = True
        return None


def _audio_langs_of(cfg: Config, chosen: Stream) -> set[str] | None:
    """The audio languages actually in `chosen`, as canonical codes — for the auto-play
    guard. Best-effort: returns None when it can't tell (no preference set, or ffprobe
    missing/empty on an untagged name), so the caller never blocks playback on a probe failure.

    Skips the ffprobe only when the release name explicitly tags the PRIMARY language — a
    fallback-only tag (e.g. ENG with primary ita) is not enough: trusting it would hide the
    primary's absence and silently play the fallback dub with no reselect/warning. A bare
    "multi"/"dual" tag is NOT trusted either: that token covers any language pair (e.g.
    Latino+Eng with no Italian at all), so both are verified with a probe. When the probe is
    impossible, a name-tagged preferred set still beats None — the guard acts on the name.
    Track languages come from `languages.track_lang`, which reads the ffprobe `title`
    (e.g. 'Italian [TrueHD]') when the `language` tag is `und`."""
    pref = set(cfg.audio_langs) | ({cfg.primary} if cfg.primary else set())
    if not pref:
        return None
    tagged = set(quality.parse_stream(chosen).languages)
    if cfg.primary and cfg.primary in tagged:
        return tagged & pref  # name explicitly tags the primary language → trust it (no probe)
    tr = tracks.probe_tracks(chosen.get("url") or "")
    if tr.empty():
        return (tagged & pref) or None  # unverifiable → fall back to the name, never block
    return {code for t in tr.audio if (code := languages.track_lang(t.lang, t.title))}


def stream_audio_langs(cfg: Config, chosen: Stream) -> frozenset[str] | None:
    """Audio languages ACTUALLY in `chosen`, by ffprobe (track-accurate, unlike the name-tag
    heuristic `parse_stream().languages` used for ranking). Resolves the url first. Returns
    None when unverifiable — no url, ffprobe missing/empty, or no identifiable track language
    (e.g. a single `und` track) — so the caller treats it as 'can't tell' and doesn't block,
    matching the local guard's benefit-of-the-doubt. Used to confirm a forced cast dub."""
    url = playable_url(cfg, chosen)
    if not url:
        return None
    tr = tracks.probe_tracks(url)
    if tr.empty():
        return None
    found = frozenset(code for t in tr.audio if (code := languages.track_lang(t.lang, t.title)))
    return found or None


def _resolve_stream(cfg: Config, chosen: Stream) -> Stream | None:
    """Make `chosen` playable: debrid/cached streams already carry a url; pure-torrent streams
    (infoHash) are resolved through the native debrid API (native backend, no P2P privacy gate)
    or, failing that, the local P2P engine. Returns None for a stream with neither url nor
    infoHash, or when every path is unservable."""
    if chosen.get("url"):
        return chosen  # debrid/cached: ready to play
    if not chosen.get("infoHash"):
        notices.emit("stream privo di url e infoHash, salto")
        return None
    native = _native_resolve(cfg, chosen)
    if native:
        chosen["url"] = native
        return chosen
    if cfg.playback_backend == "native":  # native asked but unavailable → say we degrade
        notices.emit("risoluzione debrid nativa non riuscita, ripiego su P2P…")
    try:
        # The privacy gate runs inside engine.resolve (ADR 0032), so every resolve path —
        # including `playable_url`, which the whole cast vetting runs on — is covered.
        chosen["url"] = engine.resolve(cfg, chosen)
        return chosen
    except engine.EngineUnavailable as e:
        notices.emit(f"{e}")
        _log.info("engine P2P non disponibile: %s", e)
        return None


def pick_and_resolve(
    cfg: Config,
    results: list[Stream],
    *,
    auto: bool,
    cast: bool,
    title: str = "",
    exact_resolution: int = 0,
    menu: ChooseStream | None = None,
) -> Stream | None:
    """Pick a stream and make it playable. Returns None only on ESC; raises
    `NoPlayableStream` when the pick — automatic or manual — can't be served (ADR 0033): a
    manual pick that won't resolve used to return None and read as ESC, silently."""
    chosen = _pick_stream(
        cfg, results, auto=auto, cast=cast, title=title, exact_resolution=exact_resolution,
        menu=menu,
    )  # fmt: skip
    if not chosen:
        return None
    ready = _resolve_stream(cfg, chosen)
    if ready is None:
        raise NoPlayableStream(
            unresolvable_reason(cfg, results, chosen) or "nessuna sorgente riproducibile"
        )
    return ready


def _auto_candidates(
    cfg: Config,
    results: list[Stream],
    *,
    cast: bool,
    title: str = "",
    exact_resolution: int = 0,
) -> list[Stream]:
    """Playable streams in auto-pick order (best first) — the same ranking `_pick_stream`
    uses for `auto`, exposed as a list so the language guard can try the next-best when the
    top pick lacks the primary audio language."""
    if not cfg.hw_filter:
        if exact_resolution:
            return [s for s in results if quality.parse_stream(s).resolution == exact_resolution]
        return list(results)
    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(
        cfg, cast_audio=cast, title=title, exact_resolution=exact_resolution
    )
    playable, _ = quality.rank_streams(results, caps, spec)
    return [r.stream for r in playable]


def _reselect_for_primary(
    cfg: Config, results: list[Stream], current: Stream, opts: PlayOpts, primary: str, *,
    limit: int = 4, title: str = "", exact_resolution: int = 0,
) -> Stream | None:  # fmt: skip
    """Find another candidate whose audio actually contains `primary`, spending the probe
    budget on releases whose NAME claims it: every candidate tagged `primary` (best-first,
    anywhere in the ranked list — the right dub may rank far below cached fallbacks), then
    the "multi" maybes. Each is resolved and confirmed with a real-track probe
    (`stream_audio_langs`); an unverifiable probe is accepted on benefit of the doubt only
    for an explicit `primary` tag, never for a bare "multi". Returns the first match
    (resolved, url-ready), or None when no tagged candidate qualifies within `limit`."""
    tagged: list[Stream] = []
    maybe: list[Stream] = []
    for s in _auto_candidates(
        cfg, results, cast=opts.cast, title=title, exact_resolution=exact_resolution
    ):
        if s is current or s.get("url") == current.get("url"):
            continue
        langs = quality.parse_stream(s).languages
        if primary in langs:
            tagged.append(s)
        elif "multi" in langs:
            maybe.append(s)
    if tagged or maybe:
        print(f"nstream: il pick migliore non ha audio {primary}, cerco una sorgente {primary}…",
              file=sys.stderr)  # fmt: skip
    tried = 0
    for s, trusted in [*((s, True) for s in tagged), *((s, False) for s in maybe)]:
        if tried >= limit:
            break
        tried += 1
        ready = _resolve_stream(cfg, s)
        if ready is None:
            continue  # slow/unservable (non-cached → debrid/P2P) → next candidate
        real = stream_audio_langs(cfg, ready)
        if real is None and trusted:
            real = frozenset({primary})  # unverifiable + explicit tag → benefit of the doubt
        if real and primary in real:
            name_line = next(iter((ready.get("name") or "").splitlines()), "")
            print(f"nstream: scelgo un'altra sorgente per l'audio {primary} — {name_line}",
                  file=sys.stderr)  # fmt: skip
            return ready
    return None


# --- availability orchestration (ranking picks targets; probe leaf is availability) ---


def source_key(stream: Stream) -> str:
    return availability.source_key(stream)


def prepare_candidates(cfg: Config, results: list[Stream], quality: int | None = None) -> int:
    """The candidate pass every ranking consumer shares — play, `--explain`, `--probe`
    (ADR 0037): drop sources proven removed (ADR 0025) and tag native-cached releases, in
    place. Returns the exact resolution the play path would filter to without a picker:
    `quality` (the per-invocation choice), else `cfg.default_quality`, else Auto (0). Without
    it `--explain` could describe a pick that would never play."""
    kept, _dropped = prune_dead(cfg, results)
    results[:] = kept
    _mark_native_cached(cfg, results)
    chosen = quality if quality is not None else cfg.default_quality
    return exact_resolution(chosen or 0)


def prune_dead(cfg: Config, results: list[Stream]) -> tuple[list[Stream], int]:
    return availability.prune_dead(cfg, results)


def _verify_availability(
    cfg: Config,
    results: list[Stream],
    *,
    cast: bool,
    title: str,
    exact_resolution: int = 0,
) -> list[Stream]:
    """Pre-commit availability guard (ADR 0014 + 0025; auto-pick only)."""
    if cfg.playback_backend == "local":
        return results
    targets = [
        s
        for s in _auto_candidates(
            cfg, results, cast=cast, title=title, exact_resolution=exact_resolution
        )
        if s.get("url")
    ][: availability.VERIFY_CAP]
    return availability.drop_unusable(results, targets)


def _ensure_playable(
    cfg: Config,
    results: list[Stream],
    chosen: Stream,
    opts: PlayOpts,
    *,
    title: str = "",
    exact_resolution: int = 0,
) -> Stream:
    """If the chosen debrid url is dead, fall back to P2P or the next reachable candidate."""
    if cfg.playback_backend == "local":
        return chosen
    url = chosen.get("url")
    if not url or availability.probe_stream(chosen).usable:
        return chosen
    notices.emit("la sorgente «cached» non risponde, ripiego…")
    if chosen.get("infoHash"):
        with contextlib.suppress(engine.EngineUnavailable):
            chosen["url"] = engine.resolve(cfg, chosen)
            return chosen
    tried = 0
    for s in _auto_candidates(
        cfg, results, cast=opts.cast, title=title, exact_resolution=exact_resolution
    ):
        if tried >= 3:
            break
        if s is chosen or s.get("url") == url:
            continue
        tried += 1
        ready = _resolve_stream(cfg, s)
        if ready and (not ready.get("url") or availability.probe_stream(ready).usable):
            return ready
    return chosen


def _duration_ok(cfg: Config, stream: Stream, expected_s: float) -> availability.DurationVerdict:
    """Resolve `stream`'s url (debrid/P2P) and vet its real duration. Never raises: an
    unresolvable candidate is somebody else's problem (it simply passes here)."""
    if expected_s <= 0:
        return availability.DurationVerdict(True)
    url = playable_url(cfg, stream)
    if not url:
        return availability.DurationVerdict(True, expected=expected_s)
    return availability.vet_duration(url, expected_s)


def vet_duration(
    cfg: Config,
    results: list[Stream],
    chosen: Stream,
    *,
    expected_s: float,
    cast: bool,
    title: str = "",
    exact_resolution: int = 0,
    probe_cap: int = 2,
) -> Stream:
    """Ensure `chosen` really is the video and not a 30-second "removed for copyright" clip
    (ADR 0028). Passes straight through when the duration is plausible or unverifiable.

    On a proven shortfall the candidate is dropped from `results` **in place** — so no later
    reselect (language, cast) can land back on it — and at most `probe_cap` ranked
    alternatives are resolved and probed. Raises `ContentTooShort` when none is plausible.
    `probe_cap` is deliberately smaller than the cast reselects': on P2P every extra
    candidate is another `engine.resolve` buffering wait for a title already proven fake."""
    verdict = _duration_ok(cfg, chosen, expected_s)
    if verdict.ok:
        return chosen
    notices.emit(f"sorgente troncata ({verdict.reason}), ne cerco un'altra…")
    short = [chosen]
    last = verdict
    tried = 0
    for s in _auto_candidates(cfg, results, cast=cast, title=title,
                              exact_resolution=exact_resolution):  # fmt: skip
        if tried >= probe_cap:
            break
        if any(s is bad for bad in short):
            continue
        tried += 1
        alt = _duration_ok(cfg, s, expected_s)
        if alt.ok:
            availability.drop_streams(results, short)
            return s
        short.append(s)
        last = alt
    availability.drop_streams(results, short)
    raise ContentTooShort(last, len(short))


def _mark_native_cached(cfg: Config, results: list[Stream]) -> None:
    """Native backend: batch-ask the provider which infoHashes are cached and prefix their
    `name` with the provider's `[XX+]` marker, so the existing `quality` cached signal ranks
    them first without any provider-specific code downstream. Best-effort: a provider with no
    cache check (or any error) just leaves the list unmarked (uncached streams still resolve,
    or fall back to P2P). Mutates `results` in place; idempotent via the marker check."""
    if cfg.playback_backend != "native":
        return
    resolver = debrid.get_resolver(cfg)
    if resolver is None:
        return
    hashes = [s["infoHash"].lower() for s in results if s.get("infoHash") and not s.get("url")]
    if not hashes:
        return
    try:
        cached = resolver.cached(hashes)
    except debrid.DebridUnavailable as e:
        _log.info("native cache-check non disponibile: %s", e)
        return
    marker = f"[{resolver.marker}+]"
    for s in results:
        ih = s.get("infoHash")
        if ih and ih.lower() in cached and marker not in (s.get("name") or ""):
            s["name"] = f"{marker} {s.get('name') or ''}".rstrip()


@dataclass(frozen=True)
class VettedStream:
    """The outcome of `prepare_stream`: a playable, language-vetted stream plus the
    decisions the guard may flip — `auto` (cleared when a wrong-audio warning dropped the
    user back to a manual pick), `safety_sub_lang` (set when only a fallback dub exists,
    so the player turns on primary-language subtitles as a net), and `quality` (0 = Auto /
    no filter, N = exact resolution) so a series binge can sticky the choice."""

    stream: Stream
    auto: bool
    safety_sub_lang: str | None
    quality: int = 0  # 0 = Auto; N = exact resolution filter


@log.phase("selection")
def prepare_stream(
    cfg: Config, results: list[Stream], opts: PlayOpts, *,
    auto: bool, reselect_on_wrong_audio: bool, title: str = "",
    expected_runtime_s: float = 0.0,
) -> VettedStream | None:  # fmt: skip
    """Pick one stream from `results`, resolve it, and vet it for playback. Returns the
    vetted result, or None when the user backed out (ESC) of a (re)selection — None means
    *that*, nothing else. Exhaustion raises: `NoPlayableStream` when nothing can be served,
    `ContentTooShort` when every probed source is a placeholder (ADR 0028).

    `auto` overrides `opts.auto` for this single video (the binge loop forces it True from
    the second episode on). Steps: quality resolve → pick+resolve → cached-miss fallback
    (auto only) → duration vetting (auto only) → primary-language audio guard (local mpv
    only). Cast keeps its own language UX, so the guard is skipped there.

    `expected_runtime_s` (0 = unknown → vetting off) is the title's expected playtime,
    from `api.expected_runtime_s`.

    Quality: when `opts.quality` is set (CLI / binge sticky) it hard-filters; when None and
    interactive (`reselect_on_wrong_audio`), an in-flow fzf picker offers Auto + available
    resolutions; headless / unattended defaults to Auto (no filter)."""
    # Sources proven removed in an earlier run never compete again (ADR 0025): filter before
    # ranking, not after, so a dead release can't win the auto-pick nor clutter the manual
    # picker. Idempotent — headless already pruned to answer `sources_removed`.
    engine.begin_play()  # a new play says its P2P gate verdict again, once
    # Native backend: cached releases are tagged up front so the cached score term ranks
    # them first for both the auto-pick and the cast menu. Shared with explain/probe.
    prepare_candidates(cfg, results)

    # Quality choice first (before ranking/pick) so the hard-filter is in place everywhere.
    # `reselect_on_wrong_audio` doubles as the interactive signal: True for a user-attended
    # first play, False for headless and binge-unattended advances.
    quality_choice = resolve_quality(
        cfg, results, opts, cast=opts.cast, title=title, offer_picker=reselect_on_wrong_audio
    )
    if quality_choice is None:
        return None  # ESC from the quality picker
    exact = exact_resolution(quality_choice)

    # Forced dub (`--audio-lang`): hard constraint on every path (TUI + headless, ADR 0021).
    # Fail with AudioLangUnavailable rather than falling back to another language. Skips the
    # soft primary-language guard below — the user already named the language.
    if opts.audio_lang:
        lang = opts.audio_lang
        available = tuple(audio_languages(cfg, results, cast=opts.cast, exact_resolution=exact))
        if lang not in available:
            raise AudioLangUnavailable(lang, available)
        chosen, _verified = pick_audio_stream_verified(
            cfg,
            results,
            lang,
            cast=opts.cast,
            exact_resolution=exact,
            expected_runtime_s=expected_runtime_s,
        )
        if chosen is None:
            raise AudioLangUnavailable(lang, available, real_tracks=True)
        return VettedStream(stream=chosen, auto=auto, safety_sub_lang=None, quality=quality_choice)

    # Verify the top cached candidates actually respond before committing (ADR 0014): a dead
    # `[RD+]` marker is demoted so the auto-pick re-ranks around what's live, keeping the pick
    # off a stale link (and out of an accidental Tier-2 remux). Auto only — a manual pick is
    # the user's explicit choice.
    if auto:
        _verify_availability(cfg, results, cast=opts.cast, title=title, exact_resolution=exact)
        if not results:
            # Everything url-ready was removed — headless reports sources_removed, the TUI
            # says so instead of dropping back to the menu with no explanation.
            raise NoPlayableStream(
                "tutte le sorgenti risultano non più disponibili — riprova, o azzera la "
                "denylist con --forget-dead"
            )
    # No frontend stream menu (ADR 0037) → the manual path is the automatic one, guards included.
    auto = auto or opts.choose_stream is None
    chosen = pick_and_resolve(
        cfg, results, auto=auto, cast=opts.cast, title=title, exact_resolution=exact,
        menu=opts.choose_stream,
    )  # fmt: skip
    if not chosen:
        # Auto + hard quality tier with nothing playable: fail loudly (TUI notice /
        # headless quality_unavailable). Manual ESC on the stream menu stays None.
        if auto and exact:
            raise QualityUnavailable(exact, available_resolutions(cfg, results, cast=opts.cast))
        return None

    # Cached-miss fallback (debrid/auto): a "[RD+]" marker is a guess, so verify the ready url
    # is reachable and fall back (local P2P for a hybrid stream, else the next candidate) before
    # committing to it. Only in auto mode (manual picks are the user's explicit choice).
    if auto:
        chosen = _ensure_playable(cfg, results, chosen, opts, title=title, exact_resolution=exact)

    # Truncation guard (ADR 0028): a source can be perfectly reachable and still not be the
    # video — a "removed for copyright" placeholder, or a sample inside a pack. The two
    # guards above speak for the HTTP transport only, so this is the one that covers P2P and
    # the local backend. Placed before the audio guard on purpose: it reuses the ffprobe the
    # guard is about to pay anyway, and the language reselect then starts from a vetted pick.
    if auto and expected_runtime_s:
        chosen = vet_duration(
            cfg, results, chosen, expected_s=expected_runtime_s,
            cast=opts.cast, title=title, exact_resolution=exact,
        )  # fmt: skip

    # Auto-play language guard (local mpv only): the auto-pick can be a file whose audio
    # isn't in the primary language — an untagged/mistagged foreign leak, or a "Dual"
    # release that's actually a different language pair (e.g. Latino+Eng). Verify with
    # ffprobe and act on the result instead of letting mpv silently play the wrong dub.
    safety_sub_lang: str | None = None
    if auto and not opts.cast:
        primary = cfg.primary
        avail = _audio_langs_of(cfg, chosen)
        if avail is not None and primary and primary not in avail:
            # Best pick lacks the primary language: try the next-best candidates for one
            # that has it (probing each), per the user's "try next, then fallback+subs".
            alt = _reselect_for_primary(
                cfg, results, chosen, opts, primary, title=title, exact_resolution=exact
            )
            if alt is not None:
                chosen = alt
            elif set(cfg.audio_langs) & avail:
                # Only a fallback language (e.g. eng) is available: play it, but turn on
                # primary-language subtitles as a safety net (embedded or OpenSubtitles).
                safety_sub_lang = primary
                have = "/".join(sorted(avail))
                # Report only what is known HERE: the audio fact. Whether the safety-net
                # subtitle was actually acquired is an outcome of `subs.auto_subs`, reported
                # by `subs.report_safety_subs` once the SubsPick exists — announcing it from
                # this decision site printed a promise the fetch could then contradict.
                notices.emit(
                    f"audio non disponibile in {primary} (disponibili: {have})",
                )
            else:
                # No preferred language at all: warn and (when interactive) let the user
                # pick another source with full track control.
                have = "/".join(sorted(avail)) or "?"
                notices.emit(
                    f"nessuna traccia audio {','.join(cfg.audio_langs)} (disponibili: {have})",
                )
                if reselect_on_wrong_audio:
                    auto = False  # let choose_tracks give track control on the manual pick
                    chosen = pick_and_resolve(
                        cfg,
                        results,
                        auto=False,
                        cast=opts.cast,
                        title=title,
                        exact_resolution=exact,
                        menu=opts.choose_stream,
                    )
                    if not chosen:
                        return None

    return VettedStream(
        stream=chosen, auto=auto, safety_sub_lang=safety_sub_lang, quality=quality_choice
    )
