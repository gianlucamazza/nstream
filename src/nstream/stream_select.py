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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast as typecast

from . import api, engine, languages, log, quality, tracks
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
        return f"🎬 «{title}» non ancora disponibile — uscita prevista il {released:%d/%m/%Y}"
    return f"nessuno stream disponibile per «{title}»"


def _pick_stream(
    cfg: Config, results: list[Stream], *, auto: bool, cast: bool = False
) -> Stream | None:
    """Rank and curate streams, then auto-pick the best or show an fzf menu (top N
    playable + a 'show all' entry that reveals the rest and the excluded ones ⚠).

    When `cast`, rank against the Chromecast receiver's profile (not the laptop GPU)
    and demote streams whose audio it can't decode (TrueHD/DTS/DTS-HD → silent)."""
    if not cfg.hw_filter:
        ranked = [(stream_label(s, quality.parse_stream(s)), s) for s in results]
        return results[0] if auto else fzf(ranked, "stream> ")

    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(cfg, cast_audio=cast)
    playable, excluded = quality.rank_streams(results, caps, spec)
    if excluded:
        reasons = ", ".join(sorted({r.reason for r in excluded if r.reason}))
        print(f"nstream: {len(excluded)} stream filtrati ({reasons})", file=sys.stderr)
    dupes = len(results) - len(playable) - len(excluded)
    if dupes > 0:
        print(f"nstream: {dupes} doppioni rimossi", file=sys.stderr)
    if auto:
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
        items += [(f"⚠ {r.reason}  {stream_label(r.stream, r.info)}", r.stream) for r in excluded]
        return fzf(items, "stream> ")

    cap = cfg.max_streams
    if not cap or len(playable) + len(excluded) <= cap:
        return _full()  # nothing hidden → one flat menu
    _ALL = object()
    shown = playable[:cap]
    hidden = len(playable) - len(shown) + len(excluded)
    items: list[tuple[str, object]] = [(stream_label(r.stream, r.info), r.stream) for r in shown]
    items.append((f"↓ mostra tutti ({hidden} altri)", _ALL))
    chosen = fzf(items, "stream> ")
    if chosen is _ALL:
        return _full()
    return typecast("Stream | None", chosen)


def _cast_playable(cfg: Config, results: list[Stream]) -> list[quality.RankedStream]:
    """Streams the Chromecast can play (cast profile + Cast-compatible audio), ignoring
    the language filter so every available dub is offered for switching."""
    spec = quality.FilterSpec.from_config(cfg, cast_audio=True, lang_filter=False)
    playable, _ = quality.rank_streams(results, quality.cast_caps(), spec)
    return playable


def cast_languages(cfg: Config, results: list[Stream]) -> tuple[str, ...]:
    """Audio languages available among Cast-compatible streams, preferred ones first."""
    langs = {
        lang for r in _cast_playable(cfg, results) for lang in r.info.languages if lang != "multi"
    }
    ordered = [lang for lang in cfg.audio_langs if lang in langs]
    ordered += sorted(langs - set(ordered))
    return tuple(ordered)


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


def _playable_url(cfg: Config, stream: Stream) -> str | None:
    """Ready url for a stream, resolving a pure-torrent (infoHash) one through the P2P
    engine on demand. Best-effort and silent: returns None if the engine can't serve it
    (the caller already has a user-facing fallback)."""
    if stream.get("url"):
        return stream["url"]
    if not stream.get("infoHash"):
        return None
    try:
        stream["url"] = engine.resolve(cfg, stream)
        return stream["url"]
    except engine.EngineUnavailable:
        return None


def _audio_langs_of(cfg: Config, chosen: Stream) -> set[str] | None:
    """The audio languages actually in `chosen`, as canonical codes — for the auto-play
    guard. Best-effort: returns None when it can't tell (no preference set, or ffprobe
    missing/empty), so the caller never blocks playback on a probe failure.

    Skips the ffprobe only when the release name explicitly tags a preferred language. A
    bare "multi"/"dual" tag is NOT trusted: that token covers any language pair (e.g.
    Latino+Eng with no Italian at all), so it is verified with a probe rather than assumed
    to carry a preferred track. Track languages come from `languages.track_lang`, which
    reads the ffprobe `title` (e.g. 'Italian [TrueHD]') when the `language` tag is `und`."""
    pref = set(cfg.audio_langs)
    if not pref:
        return None
    tagged = quality.parse_stream(chosen).languages
    if tagged & pref:
        return pref  # name explicitly names a preferred language → trust it (no probe)
    tr = tracks.probe_tracks(chosen.get("url") or "")
    if tr.empty():
        return None  # unverifiable → don't block
    return {code for t in tr.audio if (code := languages.track_lang(t.lang, t.title))}


def _resolve_stream(cfg: Config, chosen: Stream) -> Stream | None:
    """Make `chosen` playable: debrid/cached streams already carry a url, pure-torrent
    streams (infoHash) are resolved to a local http url by the P2P engine. Returns None
    for a stream with neither url nor infoHash, or an unservable engine."""
    if chosen.get("url"):
        return chosen  # debrid/cached: ready to play
    if not chosen.get("infoHash"):
        print("nstream: stream privo di url e infoHash, salto", file=sys.stderr)
        return None
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
    cfg: Config, results: list[Stream], *, auto: bool, cast: bool
) -> Stream | None:
    """Pick a stream and make it playable. Returns None on ESC or an unresolvable pick."""
    chosen = _pick_stream(cfg, results, auto=auto, cast=cast)
    if not chosen:
        return None
    return _resolve_stream(cfg, chosen)


def _auto_candidates(cfg: Config, results: list[Stream], *, cast: bool) -> list[Stream]:
    """Playable streams in auto-pick order (best first) — the same ranking `_pick_stream`
    uses for `auto`, exposed as a list so the language guard can try the next-best when the
    top pick lacks the primary audio language."""
    if not cfg.hw_filter:
        return list(results)
    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(cfg, cast_audio=cast)
    playable, _ = quality.rank_streams(results, caps, spec)
    return [r.stream for r in playable]


def _reselect_for_primary(
    cfg: Config, results: list[Stream], current: Stream, opts: PlayOpts, primary: str, *,
    limit: int = 3,
) -> Stream | None:  # fmt: skip
    """Try the next-best candidates (after `current`) for one whose audio actually contains
    `primary`, confirming each with a probe via `_audio_langs_of`. Returns the first match
    (resolved, url-ready), or None when none of the top `limit` others qualifies."""
    tried = 0
    for s in _auto_candidates(cfg, results, cast=opts.cast):
        if s is current or s.get("url") == current.get("url"):
            continue
        if tried >= limit:
            break
        tried += 1
        ready = _resolve_stream(cfg, s)
        if ready is None:
            continue
        avail = _audio_langs_of(cfg, ready)
        if avail and primary in avail:
            name_line = next(iter((ready.get("name") or "").splitlines()), "")
            print(f"nstream: scelgo un'altra sorgente per l'audio {primary} — {name_line}",
                  file=sys.stderr)  # fmt: skip
            return ready
    return None


def _ensure_playable(cfg: Config, results: list[Stream], chosen: Stream, opts: PlayOpts) -> Stream:
    """Debrid/auto only: the "cached" marker is a crowdsourced guess, so a ready url may be a
    dead/expired link. If the chosen url isn't reachable, fall back — to local P2P when the
    stream also carries an infoHash (hybrid 'auto'), else to the next-best reachable candidate.
    Local backend urls are engine-served (`_wait_buffer` already gates them), so skip the check."""
    if cfg.playback_backend == "local":
        return chosen
    url = chosen.get("url")
    if not url or api.url_playable(url):
        return chosen
    print("nstream: la sorgente «cached» non risponde, ripiego…", file=sys.stderr)
    if chosen.get("infoHash"):  # hybrid stream → local P2P fallback
        with contextlib.suppress(engine.EngineUnavailable):
            chosen["url"] = engine.resolve(cfg, chosen)
            return chosen
    tried = 0
    for s in _auto_candidates(cfg, results, cast=opts.cast):
        if tried >= 3:
            break
        if s is chosen or s.get("url") == url:
            continue
        tried += 1
        ready = _resolve_stream(cfg, s)
        if ready and (not ready.get("url") or api.url_playable(ready["url"])):
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
            "nstream: ⚠ nessuna VPN rilevata — in P2P il tuo IP è visibile ai peer del torrent.",
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
    auto: bool, reselect_on_wrong_audio: bool,
) -> VettedStream | None:  # fmt: skip
    """Pick one stream from `results`, resolve it, and vet it for playback. Returns the
    vetted result, or None when the user backed out (ESC) of a (re)selection.

    `auto` overrides `opts.auto` for this single video (the binge loop forces it True from
    the second episode on). Steps: pick+resolve → cached-miss fallback (auto only) → primary-
    language audio guard (local mpv only). Cast keeps its own language UX, so the guard is
    skipped there."""
    chosen = pick_and_resolve(cfg, results, auto=auto, cast=opts.cast)
    if not chosen:
        return None

    # Cached-miss fallback (debrid/auto): a "[RD+]" marker is a guess, so verify the ready url
    # is reachable and fall back (local P2P for a hybrid stream, else the next candidate) before
    # committing to it. Only in auto mode (manual picks are the user's explicit choice).
    if auto:
        chosen = _ensure_playable(cfg, results, chosen, opts)

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
            alt = _reselect_for_primary(cfg, results, chosen, opts, primary)
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
                    chosen = pick_and_resolve(cfg, results, auto=False, cast=opts.cast)
                    if not chosen:
                        return None

    return VettedStream(stream=chosen, auto=auto, safety_sub_lang=safety_sub_lang)
