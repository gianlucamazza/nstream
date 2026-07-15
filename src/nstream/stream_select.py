"""Stream selection, resolution, and the auto-play vetting guards.

Owns everything between "we have a list of streams" and "we have one playable,
language-vetted stream ready to hand to the player/caster": ranking + fzf curation,
debrid/P2P resolution, the cached-miss fallback, and the primary-language audio guard
(re-pick / safety-subtitles). `prepare_stream` is the single entry point the orchestrator
calls; the rest are module-internal helpers (also exercised directly by the tests)."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast as typecast

from . import api, debrid, engine, languages, log, quality, remux, tracks, ui
from . import config as config_mod
from .config import Config, ConfigError, PlayOpts, Stream
from .labels import stream_label
from .picker import fzf

_log = log.get_logger("stream_select")


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
    """A specific 'not released yet' notice when a title has no streams, else generic."""
    released = _future_release(api.meta(cfg, typ, video_id).get("released"))
    if released:
        return (
            f"{ui.g().movie} «{title}» non ancora disponibile — "
            f"uscita prevista il {released:%d/%m/%Y}"
        )
    return f"nessuno stream disponibile per «{title}»"


def _pick_stream(
    cfg: Config, results: list[Stream], *, auto: bool, cast: bool = False, title: str = ""
) -> Stream | None:
    """Rank and curate streams, then auto-pick the best or show an fzf menu (top N
    playable + a 'show all' entry that reveals the rest and the excluded ones ⚠).

    When `cast`, rank against the Chromecast receiver's profile (not the laptop GPU)
    and demote streams whose audio it can't decode (TrueHD/DTS/DTS-HD → silent)."""
    if not cfg.hw_filter:
        ranked = [(stream_label(s, quality.parse_stream(s)), s) for s in results]
        return results[0] if auto else fzf(ranked, "stream> ")

    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(cfg, cast_audio=cast, title=title)
    playable, excluded = quality.rank_streams(results, caps, spec)
    # Build a notice that survives into the fzf header (stderr scrolls away under fullscreen).
    notice_parts: list[str] = []
    if excluded:
        reasons = ", ".join(sorted({r.reason for r in excluded if r.reason}))
        notice_parts.append(f"{len(excluded)} stream filtrati ({reasons})")
    dupes = len(results) - len(playable) - len(excluded)
    if dupes > 0:
        notice_parts.append(f"{dupes} doppioni rimossi")
    notice = "  ·  ".join(notice_parts) if notice_parts else None
    if auto:
        # No menu → surface the ranking summary on stderr so logs still see it.
        if notice:
            print(f"nstream: {notice}", file=sys.stderr)
        if playable:
            return playable[0].stream
        msg = (
            "nessuno stream compatibile col Chromecast (prova Tab o --local)"
            if cast
            else "nessuno stream supportato dall'hardware"
        )
        print(f"nstream: {msg}", file=sys.stderr)
        return None

    def _full() -> Stream | None:
        items = [(stream_label(r.stream, r.info), r.stream) for r in playable]
        items += [
            (f"{ui.g().warn} {r.reason}  {stream_label(r.stream, r.info)}", r.stream)
            for r in excluded
        ]
        return fzf(items, "stream> ", header=notice)

    cap = cfg.max_streams
    if not cap or len(playable) + len(excluded) <= cap:
        return _full()  # nothing hidden → one flat menu
    _ALL = object()
    shown = playable[:cap]
    hidden = len(playable) - len(shown) + len(excluded)
    items: list[tuple[str, object]] = [(stream_label(r.stream, r.info), r.stream) for r in shown]
    items.append((f"{ui.g().down} mostra tutti ({hidden} altri)", _ALL))
    chosen = fzf(items, "stream> ", header=notice)
    if chosen is _ALL:
        return _full()
    return typecast("Stream | None", chosen)


def _playable_set(cfg: Config, results: list[Stream], *, cast: bool) -> list[quality.RankedStream]:
    """Streams playable on the target profile (Chromecast or local GPU), ranked best-first,
    ignoring the language filter so every available dub is visible (for listing/switching)."""
    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(cfg, cast_audio=cast, lang_filter=False)
    playable, _ = quality.rank_streams(results, caps, spec)
    return playable


def _cast_playable(cfg: Config, results: list[Stream]) -> list[quality.RankedStream]:
    """Streams the Chromecast can play (cast profile + Cast-compatible audio)."""
    return _playable_set(cfg, results, cast=True)


def audio_languages(cfg: Config, results: list[Stream], *, cast: bool) -> tuple[str, ...]:
    """Audio languages available among playable streams (local or cast profile), with the
    user's preferred languages first. Name-tag based, like the rest of the language ranking."""
    langs = {
        lang
        for r in _playable_set(cfg, results, cast=cast)
        for lang in r.info.languages
        if lang != "multi"
    }
    ordered = [lang for lang in cfg.audio_langs if lang in langs]
    ordered += sorted(langs - set(ordered))
    return tuple(ordered)


def pick_audio_stream(
    cfg: Config, results: list[Stream], lang: str, *, cast: bool
) -> Stream | None:
    """The best playable stream whose audio includes `lang` (url resolved), or None."""
    for r in _playable_set(cfg, results, cast=cast):
        if lang in r.info.languages and _playable_url(cfg, r.stream):
            return r.stream
    return None


def pick_audio_stream_verified(
    cfg: Config, results: list[Stream], lang: str, *, cast: bool, probe_cap: int = 4
) -> tuple[Stream | None, bool]:
    """Track-accurate variant: among the playable streams whose NAME tags `lang`, ffprobe up
    to `probe_cap` candidates and return the first whose REAL audio tracks carry `lang`
    (verified=True). When a candidate's real tracks are unverifiable (und/no ffprobe), accept
    it on benefit of the doubt (verified=False). Returns (None, False) only when every
    name-match's real tracks are known AND lack `lang` — i.e. the name lied for all of them."""
    name_pick: Stream | None = None
    probed = 0
    for r in _playable_set(cfg, results, cast=cast):
        if lang not in r.info.languages:
            continue
        if probed >= probe_cap:
            # Cap checked BEFORE resolving: `_playable_url` on an over-cap candidate can
            # cost a P2P buffering wait (or a debrid add) for a stream we'd discard anyway.
            break
        if not _playable_url(cfg, r.stream):
            continue
        if name_pick is None:
            name_pick = r.stream
        probed += 1
        real = stream_audio_langs(cfg, r.stream)
        if real is None:
            return r.stream, False  # unverifiable → benefit of the doubt
        if lang in real:
            return r.stream, True  # confirmed by the actual tracks
    # Every probed name-match had real tracks WITHOUT `lang` (name mistagged) → no match.
    return (None, False) if name_pick is not None and probed else (name_pick, False)


def cast_languages(cfg: Config, results: list[Stream]) -> tuple[str, ...]:
    """Audio languages available among Cast-compatible streams, preferred ones first."""
    return audio_languages(cfg, results, cast=True)


def cast_resolver(cfg: Config, results: list[Stream]) -> Callable[[str], str | None]:
    """Return a fn picking the best Cast-compatible stream URL for a language, or None.
    Closes over the already-fetched `results` so switching needs no extra network call."""
    playable = _cast_playable(cfg, results)

    def resolve(lang: str) -> str | None:
        for r in playable:  # already ranked best-first
            if lang in r.info.languages:
                return _playable_url(cfg, r.stream)
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
    """Probed audio tracks of `stream` (url resolved first), or [] when unprobeable."""
    url = _playable_url(cfg, stream)
    return list(tracks.probe_tracks(url).audio) if url else []


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
    if codes[0] == target_lang and remux._decodable(c0):
        return CastAudioPlan("direct", stream, 0, target_lang, verified=True)
    k = next((i for i, c in enumerate(codes) if c == target_lang), None)
    if k is not None:
        return CastAudioPlan("remux", stream, k, target_lang, verified=True)
    # Target language genuinely absent: the caller casts this dub anyway (+ safety subs). Its
    # default track still has to be DECODABLE — a Dolby/DTS first track would go out silent on
    # a direct cast — so flag a remux of track 0, orthogonally to the language being absent.
    return CastAudioPlan(
        "absent", stream, 0, codes[0], verified=True, needs_remux=not remux._decodable(c0)
    )


def _reselect_cast_for_lang(
    cfg: Config, results: list[Stream], current: Stream, target_lang: str, *, probe_cap: int = 6
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
    probed = 0
    for r in _cast_playable(cfg, results):
        s = r.stream
        if s is current or s.get("url") == current.get("url"):
            continue
        langs = r.info.languages
        if target_lang not in langs and "multi" not in langs:  # only chase claimed-language dubs
            continue
        if probed >= probe_cap:
            break
        probed += 1
        plan = _cast_plan_for(s, _cast_audio_tracks(cfg, s), target_lang)
        if plan.mode == "direct" and plan.verified:
            return plan  # cheapest *verified* correct option (no download) → take it
        if plan.mode == "remux" and remux_fallback is None:
            remux_fallback = plan  # remember, but keep looking for a direct one
        elif (
            plan.mode == "direct"
            and not plan.verified
            and target_lang in langs  # the NAME explicitly claims it (not just "multi")
            and tagged_guess is None
        ):
            # Unprobeable but explicitly target-tagged → a benefit-of-the-doubt last resort,
            # kept only if no verified option turns up (below any remux_fallback).
            tagged_guess = plan
    return remux_fallback or tagged_guess


def vet_cast_audio(
    cfg: Config, results: list[Stream], chosen: Stream, target_lang: str
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
    return _reselect_cast_for_lang(cfg, results, chosen, target_lang) or plan


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


def _playable_url(cfg: Config, stream: Stream) -> str | None:
    """Ready url for a stream, resolving a pure-torrent (infoHash) one through the native
    debrid API (native backend) or the P2P engine on demand. Best-effort and silent: returns
    None if neither can serve it (the caller already has a user-facing fallback)."""
    if stream.get("url"):
        return stream["url"]
    if not stream.get("infoHash"):
        return None
    native = _native_resolve(cfg, stream)
    if native:
        stream["url"] = native
        return native
    try:
        stream["url"] = engine.resolve(cfg, stream)
        return stream["url"]
    except engine.EngineUnavailable:
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
    url = _playable_url(cfg, chosen)
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
        print("nstream: stream privo di url e infoHash, salto", file=sys.stderr)
        return None
    native = _native_resolve(cfg, chosen)
    if native:
        chosen["url"] = native
        return chosen
    if cfg.playback_backend == "native":  # native asked but unavailable → say we degrade
        print("nstream: risoluzione debrid nativa non riuscita, ripiego su P2P…", file=sys.stderr)
    if not _p2p_guard(cfg):
        return None
    try:
        chosen["url"] = engine.resolve(cfg, chosen)
        return chosen
    except engine.EngineUnavailable as e:
        print(f"nstream: {e}", file=sys.stderr)
        _log.info("engine P2P non disponibile: %s", e)
        return None


def pick_and_resolve(
    cfg: Config, results: list[Stream], *, auto: bool, cast: bool, title: str = ""
) -> Stream | None:
    """Pick a stream and make it playable. Returns None on ESC or an unresolvable pick."""
    chosen = _pick_stream(cfg, results, auto=auto, cast=cast, title=title)
    if not chosen:
        return None
    return _resolve_stream(cfg, chosen)


def _auto_candidates(
    cfg: Config, results: list[Stream], *, cast: bool, title: str = ""
) -> list[Stream]:
    """Playable streams in auto-pick order (best first) — the same ranking `_pick_stream`
    uses for `auto`, exposed as a list so the language guard can try the next-best when the
    top pick lacks the primary audio language."""
    if not cfg.hw_filter:
        return list(results)
    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(cfg, cast_audio=cast, title=title)
    playable, _ = quality.rank_streams(results, caps, spec)
    return [r.stream for r in playable]


def _reselect_for_primary(
    cfg: Config, results: list[Stream], current: Stream, opts: PlayOpts, primary: str, *,
    limit: int = 4, title: str = "",
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
    for s in _auto_candidates(cfg, results, cast=opts.cast, title=title):
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


# Process-lifetime memo of the reachability probe (like the parse/ffprobe memos): a resolved
# url's availability doesn't change within a run, so probe each at most once — shared by the
# pre-commit cached verification and `_ensure_playable`'s last-resort net, so a url the
# verifier already found live isn't re-probed when the pick is confirmed.
_PROBE_MEMO: dict[str, bool] = {}
_VERIFY_CACHED_CAP = 5  # top-N cached candidates to probe before committing the auto-pick


def _probe_url(url: str) -> bool:
    """Memoized `api.url_playable` for a resolved stream url (see `_PROBE_MEMO`)."""
    verdict = _PROBE_MEMO.get(url)
    if verdict is None:
        verdict = api.url_playable(url)
        _PROBE_MEMO[url] = verdict
    return verdict


def _demote_cached(stream: Stream) -> None:
    """Strip the debrid cached marker (`[RD+]`, `[TB+]`…) from a stream's name so
    `quality.parse_stream` no longer ranks it as instantly available — its ready url probed
    dead. The inverse of `_mark_native_cached`: the demotion then flows through the whole
    ranking/explain pipeline with no downstream special-casing (parse_stream re-keys on the
    new name). Provider-agnostic via the shared `quality._CACHED_RE`."""
    name = stream.get("name") or ""
    stripped = quality._CACHED_RE.sub("", name).strip()
    if stripped != name:
        stream["name"] = stripped


def _verify_cached_availability(
    cfg: Config, results: list[Stream], *, cast: bool, title: str
) -> None:
    """Pre-commit availability guard (ADR 0014, auto-pick only): the Torrentio `[RD+]` cached
    marker is a crowdsourced guess that can be stale/evicted, yet `cached` is the top-precedence
    rank term — so a dead cached release wins the auto-pick and only `_ensure_playable` catches
    it, after cascading through resolves and possibly landing in an expensive Tier-2 remux.

    Instead, probe the real reachability of the top-N ranked *cached* candidates concurrently
    and demote any dead one to uncached-equivalent (strip its marker) so the very next rank pass
    re-orders around what actually responds — a live 1080p AAC release then outranks a dead 4K
    cached one. Bounded (≤`_VERIFY_CACHED_CAP`) and memoized (each url probed once, reused by
    `_ensure_playable`). Only cached candidates are probed; uncached ones are already gated by
    their seeder count. No-op for the local backend (engine-served urls are buffer-gated, not
    cached-marked). Mutates `results` in place."""
    if cfg.playback_backend == "local":
        return
    targets = [
        s
        for s in _auto_candidates(cfg, results, cast=cast, title=title)
        if s.get("url") and quality.parse_stream(s).cached
    ][:_VERIFY_CACHED_CAP]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=min(len(targets), _VERIFY_CACHED_CAP)) as ex:
        verdicts = list(ex.map(lambda s: _probe_url(s["url"]), targets))
    for s, live in zip(targets, verdicts, strict=True):
        if not live:
            _demote_cached(s)


def _ensure_playable(
    cfg: Config, results: list[Stream], chosen: Stream, opts: PlayOpts, *, title: str = ""
) -> Stream:
    """Debrid/auto only: the "cached" marker is a crowdsourced guess, so a ready url may be a
    dead/expired link. If the chosen url isn't reachable, fall back — to local P2P when the
    stream also carries an infoHash (hybrid 'auto'), else to the next-best reachable candidate.
    Local backend urls are engine-served (`_wait_buffer` already gates them), so skip the check.

    Last-resort net after `_verify_cached_availability` (which already re-ranked around dead
    cached links up front): this still runs so a url that dies between probe and play, or the
    non-auto paths, are covered. Shares the `_probe_url` memo, so a candidate already probed
    live by the verifier isn't hit twice."""
    if cfg.playback_backend == "local":
        return chosen
    url = chosen.get("url")
    if not url or _probe_url(url):
        return chosen
    print("nstream: la sorgente «cached» non risponde, ripiego…", file=sys.stderr)
    if chosen.get("infoHash"):  # hybrid stream → local P2P fallback
        with contextlib.suppress(engine.EngineUnavailable):
            chosen["url"] = engine.resolve(cfg, chosen)
            return chosen
    tried = 0
    for s in _auto_candidates(cfg, results, cast=opts.cast, title=title):
        if tried >= 3:
            break
        if s is chosen or s.get("url") == url:
            continue
        tried += 1
        ready = _resolve_stream(cfg, s)
        if ready and (not ready.get("url") or _probe_url(ready["url"])):
            return ready
    return chosen  # nothing better reachable — let the player try anyway


def _p2p_guard(cfg: Config) -> bool:
    """Privacy gate before serving a P2P stream. Returns False — blocking playback — only when
    `p2p_require_vpn` is set and no VPN interface is detected; otherwise warns (when no VPN) and
    proceeds. BP: P2P joins the swarm, so without a VPN the real IP is visible to peers."""
    if not engine.vpn_active():
        if cfg.p2p_require_vpn:
            print(
                "nstream: nessuna VPN rilevata e p2p_require_vpn=true — streaming P2P bloccato.\n"
                "         Attiva la VPN, oppure usa un provider debrid.",
                file=sys.stderr,
            )
            return False
        print(
            f"nstream: {ui.g().warn} nessuna VPN rilevata — "
            "in P2P il tuo IP è visibile ai peer del torrent.",
            file=sys.stderr,
        )
    _p2p_notice_once(cfg)
    return True


def _p2p_notice_once(cfg: Config) -> None:
    """One-time privacy notice the first time a P2P stream is served: torrent peers see the
    client's IP. Persists the acknowledgement so it isn't shown again; never blocks playback."""
    if cfg.p2p_ack:
        return
    print(
        "nstream: streaming P2P locale attivo — il tuo IP è visibile ai peer del torrent.\n"
        "         Valuta una VPN se è una preoccupazione. (avviso mostrato una sola volta)",
        file=sys.stderr,
    )
    with contextlib.suppress(ConfigError, OSError):
        config_mod.save({"p2p_ack": True})


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
    """The outcome of `prepare_stream`: a playable, language-vetted stream plus the two
    decisions the guard may flip — `auto` (cleared when a wrong-audio warning dropped the
    user back to a manual pick) and `safety_sub_lang` (set when only a fallback dub exists,
    so the player turns on primary-language subtitles as a net)."""

    stream: Stream
    auto: bool
    safety_sub_lang: str | None


def prepare_stream(
    cfg: Config, results: list[Stream], opts: PlayOpts, *,
    auto: bool, reselect_on_wrong_audio: bool, title: str = "",
) -> VettedStream | None:  # fmt: skip
    """Pick one stream from `results`, resolve it, and vet it for playback. Returns the
    vetted result, or None when the user backed out (ESC) of a (re)selection.

    `auto` overrides `opts.auto` for this single video (the binge loop forces it True from
    the second episode on). Steps: pick+resolve → cached-miss fallback (auto only) → primary-
    language audio guard (local mpv only). Cast keeps its own language UX, so the guard is
    skipped there."""
    # Native backend: tag cached releases up front so the cached score term ranks them first
    # for both the auto-pick and the cast menu (mutates `results` once, in place).
    _mark_native_cached(cfg, results)
    # Verify the top cached candidates actually respond before committing (ADR 0014): a dead
    # `[RD+]` marker is demoted so the auto-pick re-ranks around what's live, keeping the pick
    # off a stale link (and out of an accidental Tier-2 remux). Auto only — a manual pick is
    # the user's explicit choice.
    if auto:
        _verify_cached_availability(cfg, results, cast=opts.cast, title=title)
    chosen = pick_and_resolve(cfg, results, auto=auto, cast=opts.cast, title=title)
    if not chosen:
        return None

    # Cached-miss fallback (debrid/auto): a "[RD+]" marker is a guess, so verify the ready url
    # is reachable and fall back (local P2P for a hybrid stream, else the next candidate) before
    # committing to it. Only in auto mode (manual picks are the user's explicit choice).
    if auto:
        chosen = _ensure_playable(cfg, results, chosen, opts, title=title)

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
            alt = _reselect_for_primary(cfg, results, chosen, opts, primary, title=title)
            if alt is not None:
                chosen = alt
            elif set(cfg.audio_langs) & avail:
                # Only a fallback language (e.g. eng) is available: play it, but turn on
                # primary-language subtitles as a safety net (embedded or OpenSubtitles).
                safety_sub_lang = primary
                have = "/".join(sorted(avail))
                print(
                    f"nstream: audio non disponibile in {primary} (disponibili: {have}); "
                    f"sottotitoli {primary} attivati",
                    file=sys.stderr,
                )
            else:
                # No preferred language at all: warn and (when interactive) let the user
                # pick another source with full track control.
                have = "/".join(sorted(avail)) or "?"
                print(
                    f"nstream: nessuna traccia audio {','.join(cfg.audio_langs)} "
                    f"(disponibili: {have})",
                    file=sys.stderr,
                )
                if reselect_on_wrong_audio:
                    auto = False  # let choose_tracks give track control on the manual pick
                    chosen = pick_and_resolve(cfg, results, auto=False, cast=opts.cast, title=title)
                    if not chosen:
                        return None

    return VettedStream(stream=chosen, auto=auto, safety_sub_lang=safety_sub_lang)
