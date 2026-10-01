"""Shared cast decision tree: vet the audio plan, then mirror / Tier-2 remux / direct cast.

`run_cast` is the single body behind the interactive cast (`cli._play_on_cast`) and the
headless `--json --cast` branch (`cli._auto_play`), which used to carry two hand-synced
copies of the same vet → mirror-gate → remux → direct sequence. Same tier as
`stream_select`: sits below `cli` (never imports it) and orchestrates the cast backends.

Three deliberate normalizations vs the historical copies (everything else is a 1:1 port):
  1. the "absent dub" stderr notice (audio X unavailable → safety subtitles) is printed on
     BOTH paths (it used to be interactive-only);
  2. `auto_subs` runs exactly ONCE (the headless copy used to call it twice in the absent
     branch: pre-decision, then again with the safety language);
  3. the mirror-fallback notice is a single constant.

The cast is also vetted for CONTAINER (ADR 0022): the DMR refuses .mkv on a direct cast, so
`vet_cast_container` reselects an MP4 twin or routes the pick through the Tier-2 copy/copy
rewrap to MP4. And an explicit `--mirror` now forces the mirror even for a DMR-decodable
title (ADR 0023); it only downgrades to a direct cast when the mirror backend is unavailable.

What stays in the callers: device resolution, the headless volume guard (gated on
`CastOutcome.action == "cast"`), and the JSON protocol/accounting. The per-cast
`_log.info` line moved here, so the headless path now logs the cast decision too.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace

from . import (
    cast_delivery,
    cast_vet,
    caster,
    engine,
    log,
    mirror,
    picker,
    quality,
    remux,
    state,
    stream_select,
    subs,
    ui,
)
from .config import Config, PlayOpts
from .types import Stream

_log = log.get_logger("cast_flow")

# Printed when `--mirror` is forced but the mirror backend isn't available (ADR 0023): an
# explicit --mirror otherwise forces the mirror even for a DMR-decodable title.
MIRROR_UNAVAILABLE = "nstream: mirror non disponibile → cast diretto"


def _cast_would_wait(cfg: Config, plan: cast_vet.CastAudioPlan) -> bool:
    """True when delivering `plan` fetches a complete file before the TV can start.

    Remux and an undecodable track always wait. A direct plan waits only when the
    container itself is not loadable (MKV rewrap, ADR 0022)."""
    if plan.mode == "remux" or plan.needs_remux:
        return True
    if not plan.stream.get("url"):
        return False
    return not quality.container_castable(cast_vet.cast_container(cfg, plan.stream))


def _prepare_line(stream: Stream, plan: cast_vet.CastAudioPlan, target: str) -> str:
    """One non-tty-safe line before a full-file prepare (ADR 0035)."""
    info = quality.parse_stream(stream)
    lang = plan.real_lang or target or "?"
    size = f", ~{info.size_gb:.1f}GB" if info.size_gb else ""
    box = info.container or "?"
    return (
        f"nstream: preparo il file intero ({box}{size}, audio {lang}) "
        "— la TV parte a preparazione finita"
    )


class CastStreamUnresolved(Exception):
    """The settled cast stream has no url: nothing could resolve it (dead swarm, or a debrid
    still transferring the file). Raised once at the settle point instead of letting the four
    delivery branches dereference a missing key (ADR 0031 appendix). Callers surface it as
    the retry-worthy `no_playable_stream`, never as an internal crash."""

    def __init__(self) -> None:
        super().__init__("nessuna sorgente castabile risolvibile ora")


class CastVideoUnsupported(Exception):
    """No cast candidate carries video the DMR can render, and the mirror fallback is not
    available (ADR 0017). Casting anyway would play black with state PLAYING and no receiver
    error — the callers surface this instead (headless: `video_codec_unsupported`)."""

    def __init__(self, codec: str):
        self.codec = codec
        super().__init__(
            f"video {codec} non decodificabile dal Chromecast e nessuna release alternativa"
        )


class CastRemuxInfeasible(Exception):
    """The pick needs a Tier-2 remux (undecodable audio / .mkv), the remux would be refused
    (disk, size cap, ffmpeg, config), and neither the mirror nor a verified direct release
    can stand in. Casting the file as-is plays mute or not at all, yet used to report
    `ok: true` (2026-10-01) — the callers surface this instead (headless:
    `remux_infeasible`)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"remux non fattibile: {reason}")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


# Resolution (px height) at/above which a release is 4K/UHD: a remux of one is a tens-of-GB
# fetch regardless of the parsed size, so it trips the mirror-over-remux rule even when the
# size is unlabelled. Matches the cast resolution ceiling (`quality.cast_caps`).
_UHD_MIN_RESOLUTION = 2160


def _remux_is_pathological(info: quality.StreamInfo, threshold_gb: int) -> bool:
    """Whether a Tier-2 remux of this release would be a pathological multi-GB fetch — the
    trigger for auto-preferring the mirror (ADR 0015). True when the parsed size is ≥
    `threshold_gb`, or (size unknown) the release is 4K/8K, where a remux means tens of GB
    regardless — the case the resolution can't let the size hide. `threshold_gb == 0` disables
    the auto-switch entirely (always remux)."""
    if threshold_gb <= 0:
        return False
    if info.size_gb:
        return info.size_gb >= threshold_gb
    return info.resolution >= _UHD_MIN_RESOLUTION


@dataclass(frozen=True)
class CastOutcome:
    """What the cast decision tree did, for the caller's accounting (resume save,
    `--json` fields). `stream` is the post-vet stream (a reselected dub when the
    original first track couldn't carry the target language)."""

    pos: float
    dur: float
    advance: bool
    action: str  # "cast" | "mirror"
    stream: Stream
    reencoded: bool  # True when a Tier-2 audio remux was used
    notice: str | None  # remux-degraded warning (the `--json` notice field)
    audio_lang: str | None  # plan.real_lang: what actually plays, or None when unknown
    audio_verified: bool  # True when decided from real ffprobe tracks
    safety_sub_lang: str | None  # effective safety-subtitle language, or None
    sub_paths: tuple[str, ...]
    sub_match: str | None  # "hash" | "audio" (local-media aligned) | "lang" | None
    sub_offset: float | None  # measured+applied correction when sub_match == "audio"
    subs_delivered: bool  # False when the delivery couldn't attach them (castbridge LOAD)
    started: bool  # the backend observed the handoff (ADR 0031) — `ok: true` requires it
    cast_error: str | None  # backend failure code when not started ("cast_failed", …)
    # The planned remux failed and the file went out as-is: what plays is unknown (maybe
    # mute). Callers must not fall back to a pre-cast language guess.
    audio_degraded: bool = False


def run_cast(
    cfg: Config,
    results: list[Stream],
    chosen: Stream,
    *,
    device: str,
    title: str,
    typ: str,
    video_id: str,
    work_dir: str,
    opts: PlayOpts,
    start: float | None,
    follow: bool = True,
    next_label: str | None = None,
    allow_lang_switch: bool = False,
    meta: caster.CastMeta | None = None,
    on_event: caster.EventCb | None = None,
    safety_sub_lang: str | None = None,
    expected_runtime_s: float = 0.0,
) -> CastOutcome:
    """Cast `chosen` to `device` in the target audio language. The Default Media Receiver
    plays a file's first audio track and can't switch tracks, so the language is enforced
    at selection time (`vet_cast_audio`): cast directly when the first track is already
    target + decodable, remux to select the track otherwise, or reselect a dub that has it;
    no dub at all → cast the best pick with target-language safety subtitles.

    `allow_lang_switch` wires the interactive in-cast audio switch ('a': langs + resolver,
    an extra rank pass) — leave it False on headless paths. `follow=False` (headless
    fire-and-return) also detaches a TorrServer the engine may have spawned, so the TV
    keeps streaming past process exit.

    `expected_runtime_s` (0 = unknown) keeps every reselect below off placeholder/sample
    files (ADR 0028) — the same guard `prepare_stream` already applied to `chosen`."""
    # Video first (ADR 0017): a video codec the DMR can't render casts as PLAYING + black
    # screen with no receiver error, so the REAL codec is verified before any side effect.
    # No castable candidate and no mirror to decode locally → explicit failure, not a black cast.
    # ADR 0021: the resolved per-invocation quality (opts.quality, always an int here —
    # the callers replace() it with VettedStream.quality) constrains EVERY reselect below.
    exact = stream_select.exact_resolution(opts.quality or 0)
    chosen, bad_video = cast_vet.vet_cast_video(
        cfg, results, chosen, exact_resolution=exact, expected_s=expected_runtime_s
    )
    # --no-mirror is an explicit user intent: with undecodable video and the mirror
    # suppressed, fail explicitly rather than override the user (ADR 0021).
    if bad_video and (not mirror.available() or opts.mirror is False):
        raise CastVideoUnsupported(bad_video)
    # A new cast replaces the TV's content: a previous fire-and-return session no longer
    # describes it (the headless caller re-writes a fresh one right after this returns).
    state.clear_cast_session()
    target_lang = opts.audio_lang or cfg.primary
    # Container gate (ADR 0022): the DMR refuses .mkv on a direct cast though it decodes the
    # same HEVC/AAC in .mp4. Prefer an MP4 twin (a free direct cast) over a download+rewrap;
    # this is the optimization, the settled-stream check below is the guarantee.
    pre_container = chosen
    chosen, _bad_container = cast_vet.vet_cast_container(
        cfg, results, chosen, target_lang, exact_resolution=exact, expected_s=expected_runtime_s
    )
    if chosen is not pre_container:
        bad_video = ""  # a vetted MP4 candidate supersedes the original's video verdict
    plan = cast_vet.vet_cast_audio(
        cfg, results, chosen, target_lang, exact_resolution=exact, expected_s=expected_runtime_s
    )
    # ADR 0035: a soft language preference must not force a full-file fetch when a
    # verified direct cast exists at this quality. Explicit `--audio-lang` stays hard.
    # Headless takes the direct cast and reports it; a TUI asks once before dropping
    # the primary dub.
    notice_defer: str | None = None
    if not opts.audio_lang and target_lang and _cast_would_wait(cfg, plan):
        instant = cast_vet.find_instant_direct(
            cfg,
            results,
            tuple(cfg.audio_langs),
            exact_resolution=exact,
            expected_s=expected_runtime_s,
        )
        if instant is not None and instant.stream is not plan.stream:
            take = instant.real_lang == target_lang
            if not take:
                if sys.stdin.isatty() and sys.stderr.isatty():
                    take = picker.confirm(
                        f"«{target_lang}» solo dopo il download completo. "
                        f"Parto subito in {instant.real_lang}?",
                        default_yes=True,
                        non_tty_default=True,
                    )
                else:
                    take = True
            if take:
                if instant.real_lang != target_lang:
                    notice_defer = cast_vet.instant_defer_notice(plan, instant, target_lang)
                    print(f"nstream: {notice_defer}", file=sys.stderr)
                    if not opts.sub_lang:
                        safety_sub_lang = target_lang
                else:
                    print(
                        "nstream: release diretta nella stessa lingua, salto il remux",
                        file=sys.stderr,
                    )
                plan = instant
    if plan.stream is not chosen:
        # The language reselect only offers video-castable candidates (its guard shares
        # this vetting), so a swap clears the bad-video verdict along with the stream.
        bad_video = ""
    chosen = plan.stream
    # The stream has settled: from here every branch dereferences `chosen["url"]`. Assert it
    # once, honestly, instead of four `.get()`s that would each cast a url-less stream a
    # different wrong way (ADR 0031 appendix — this is the KeyError: 'url' crash site).
    if not chosen.get("url"):
        raise CastStreamUnresolved()
    # Remux is a codec decision, orthogonal to language availability: `remux` mode selects a
    # target-language track, but an `absent` fallback whose default track is Dolby/DTS must be
    # remuxed too, or it casts silent (the DMR can't decode it). `plan.needs_remux` carries that.
    # A DMR-incompatible container (.mkv) on the SETTLED stream also forces the Tier-2 rewrap to
    # MP4 (ADR 0022) — the guarantee that no path hands the DMR an .mkv LOAD, whichever release
    # the audio reselect landed on. A decodable-audio rewrap is `-c:v copy -c:a copy` (remux.py).
    final_container = cast_vet.cast_container(cfg, chosen)
    needs_rewrap = not quality.container_castable(final_container)
    needs_remux = plan.mode == "remux" or plan.needs_remux or needs_rewrap
    # Decide feasibility BEFORE committing to a whole-file prepare: a refused remux used to
    # degrade to a direct cast of the undecodable file (mute TV, `ok: true`). Order of
    # stand-ins: the mirror (decodes locally), a verified direct release, else fail honestly.
    refused_mirror = ""
    if needs_remux and not opts.mirror:
        why = remux.refusal(cfg, quality.parse_stream(chosen).size_gb, interactive=_interactive())
        if why:
            mirror_ok_now = mirror.available()
            if opts.mirror is None and mirror_ok_now:
                refused_mirror = why
            else:
                langs = (opts.audio_lang,) if opts.audio_lang else tuple(cfg.audio_langs)
                alt = cast_vet.find_instant_direct(
                    cfg, results, langs, exact_resolution=exact, expected_s=expected_runtime_s
                )
                if alt is None or alt.stream is chosen:
                    if not mirror_ok_now and opts.mirror is None:
                        why = f"{why}; mirror non disponibile ({mirror.unavailable_reason()})"
                    raise CastRemuxInfeasible(why)
                notice_defer = f"{why} → cast diretto {alt.real_lang or '?'}"
                print(f"nstream: {notice_defer}", file=sys.stderr)
                plan, chosen, bad_video = alt, alt.stream, ""
                if not chosen.get("url"):
                    raise CastStreamUnresolved()
                final_container = cast_vet.cast_container(cfg, chosen)
                needs_rewrap = needs_remux = False
    # No dub carries the target language: cast the best pick anyway, with target-language
    # subtitles as a safety net (mirrors the local guard). Printed on both paths (norm. 1).
    # An EXPLICIT `--sub-lang` wins over this primary-language safety default: the user asked
    # for a specific subtitle language, so honour it (still warn that the audio isn't primary).
    if plan.mode == "absent" and target_lang:
        real = f" (casto {plan.real_lang})" if plan.real_lang else ""
        if not opts.sub_lang:
            safety_sub_lang = target_lang
        # The audio fact is known here; the safety-net subtitle outcome is not, and is
        # reported by `subs.report_safety_subs` below (same rule as the local path).
        print(f"nstream: audio {target_lang} non disponibile{real}", file=sys.stderr)
    # Exactly one auto_subs call, with the effective safety language (normalization 2).
    # The resolved url/filename enable the exact-file hash match (ADR 0018).
    subs_pick = subs.auto_subs(
        cfg, typ, video_id, work_dir, opts, safety_sub_lang=safety_sub_lang,
        video_url=chosen["url"], filename=subs.stream_filename(chosen),
    )  # fmt: skip
    subs.report_safety_subs(subs_pick, safety_sub_lang)
    sub_paths = subs_pick.paths
    # Language of the fetched subtitle track (labels the side-loaded caption track on the TV).
    sub_lang = safety_sub_lang or opts.sub_lang
    _log.info("cast '%s' → %s (%s/%s)", title, device, plan.mode, plan.real_lang or "?")

    notice: str | None = notice_defer
    reencoded = False
    degraded_audio = False
    # Backend strategy. The DMR plays AAC/HEVC/4K/HDR natively and instantly — strictly better
    # than the mirror (1080p SDR re-encode, latency) — so mirroring only helps when the audio is
    # one the DMR can't decode (`needs_remux`): there mpv decodes Dolby/DTS locally and starts
    # instantly, beating the remux prepare-wait. It is chosen when EITHER the user forced it
    # (`--mirror`/`cast_mode`), OR the remux would be a pathological multi-GB fetch (a 4K
    # Dolby-only pick) and the mirror is available (ADR 0015): instant start + no download beats
    # a 30-60 GB fetch, at the cost of 1080p SDR. Below the size threshold the remux still wins
    # (native video/HDR). An EXPLICIT `--mirror` now forces the mirror even for a decodable
    # title (ADR 0023): the manual intent wins, mirroring how `--no-mirror` suppresses it.
    info = quality.parse_stream(chosen)
    # `bad_video` forces the mirror regardless of audio: mpv decodes the legacy video
    # locally, the only remaining way to show this title on the TV (availability was
    # already vetted above — reaching here with `bad_video` implies mirror.available()).
    force_mirror = bool(bad_video) or bool(refused_mirror)
    mirror_ok = (needs_remux or force_mirror or opts.mirror is True) and mirror.available()
    # Tri-state opts.mirror (ADR 0021): the ADR-0015 auto-switch applies only when the
    # user expressed NO per-invocation preference (None); --no-mirror (False) suppresses
    # it without touching the global config knob.
    auto_mirror = (
        mirror_ok
        and opts.mirror is None
        and _remux_is_pathological(info, cfg.cast_mirror_over_remux_gb)
    )
    if mirror_ok and (opts.mirror is True or auto_mirror or force_mirror):
        if refused_mirror and not bad_video:
            notice = f"{refused_mirror} → mirror 1080p (decodifica locale)"
            print(f"nstream: {notice}", file=sys.stderr)
        elif force_mirror:
            notice = (
                f"video {bad_video.upper()} non decodificabile dal TV"
                " → mirror 1080p (decodifica locale)"
            )
            print(f"nstream: {notice}", file=sys.stderr)
        elif auto_mirror:
            size = f" (~{info.size_gb:.0f} GB)" if info.size_gb else ""
            notice = (
                f"remux troppo pesante{size} → mirror 1080p SDR "
                f"(soglia {cfg.cast_mirror_over_remux_gb} GB; avvio immediato)"
            )
            print(f"nstream: {notice}", file=sys.stderr)
            ui.status(notice, kind="tv")
        delivery = mirror.cast_via_mirror(
            cfg, title, chosen["url"],
            device=device, start=start, sub_paths=sub_paths, follow=follow,
        )  # fmt: skip
        pos, dur = delivery.pos, delivery.dur
        subs_delivered = bool(sub_paths)  # mpv renders them into the mirrored frame
        action = "mirror"
    else:
        if opts.mirror is True:
            # Reached the else with --mirror forced ⇒ `mirror.available()` is False (the only
            # way `mirror_ok` is False here): honour the intent with an honest fallback notice.
            print(MIRROR_UNAVAILABLE, file=sys.stderr)
        if needs_remux:
            print(_prepare_line(chosen, plan, target_lang), file=sys.stderr)
        remux_path = (
            remux.remux_for_cast(
                chosen["url"],
                cfg,
                audio_index=plan.audio_index,
                size_gb=quality.parse_stream(chosen).size_gb,
            )  # fmt: skip
            if needs_remux
            else None
        )
        if remux_path:
            # Tier 2 of the subtitle pipeline (ADR 0020): the remux output IS the local
            # media file the receiver will play — align the delivered subtitle against
            # its real audio (free of network cost) before the VTT is built from it.
            subs_pick = subs.align_local(cfg, subs_pick, remux_path, work_dir, opts)
            sub_paths = subs_pick.paths
            delivery = remux.cast_file(
                cfg, title, remux_path,
                device=device, start=start, sub_paths=sub_paths, sub_lang=sub_lang, follow=follow,
                meta=meta, on_event=on_event,
            )  # fmt: skip
            pos, dur, subs_delivered = delivery.pos, delivery.dur, delivery.subs_delivered
            reencoded = True
            action = "cast"
        elif needs_rewrap and mirror.available():
            # ADR 0022 gap: the container rewrap is unavailable (cfg.cast_remux off or ffmpeg
            # missing) and the DMR refuses this .mkv LOAD — mirror it (mpv decodes any
            # container) instead of a silent black direct cast.
            notice = "rewrap non disponibile → mirror 1080p (il TV non carica questo container)"
            print(f"nstream: {notice}", file=sys.stderr)
            delivery = mirror.cast_via_mirror(
                cfg, title, chosen["url"],
                device=device, start=start, sub_paths=sub_paths, follow=follow,
            )  # fmt: skip
            pos, dur = delivery.pos, delivery.dur
            subs_delivered = bool(sub_paths)
            action = "mirror"
        else:
            if needs_remux:
                # remux failed mid-way (ffmpeg error, or a guard tripped after `refusal`)
                # → direct cast of a file whose first audio track is Dolby (silent on the
                # DMR) or the wrong dub. The planned track no longer describes what plays.
                notice = (
                    "remux non riuscito → cast diretto: l'audio potrebbe "
                    "risultare muto o in un'altra lingua"
                )
                print(f"nstream: {ui.g().warn} {notice}", file=sys.stderr)
                degraded_audio = True
            # Declare the container's MIME on the LOAD instead of leaving the DMR to sniff
            # (ADR 0022): reaching a direct cast means the container is DMR-compatible
            # (mp4/webm) or unknown; set contentType when known so the receiver doesn't guess.
            mime = quality.container_mime(final_container)
            if mime and not (meta and meta.content_type):
                meta = replace(meta or caster.CastMeta(), content_type=mime)
            # In-cast audio switch ('a'): only the interactive path pays the extra rank
            # passes; headless callers leave allow_lang_switch False.
            langs: tuple[str, ...] = ()
            resolver = None
            if allow_lang_switch:
                cast_langs = cast_vet.cast_languages(cfg, results, exact_resolution=exact)
                if len(cast_langs) > 1:
                    langs = cast_langs
                    resolver = cast_vet.cast_resolver(cfg, results, exact_resolution=exact)
            delivery = caster.cast(
                cfg, title, chosen["url"],
                device=device, start=start, sub_paths=sub_paths, sub_lang=sub_lang,
                langs=langs, resolve_lang=resolver, follow=follow,
                meta=meta, on_event=on_event,
            )  # fmt: skip
            pos, dur, subs_delivered = delivery.pos, delivery.dur, delivery.subs_delivered
            action = "cast"
    if sub_paths and not subs_delivered:
        # Honesty over silence: the subtitles were fetched but not attached to the cast
        # (e.g. WebVTT conversion/serving failed, or the mirror path with no burn-in).
        print(
            f"nstream: {ui.g().warn} sottotitoli scaricati ma non caricati sul TV",
            file=sys.stderr,
        )
    if not follow:
        # Fire-and-return handoff: a pure-torrent stream is served by the TorrServer we may
        # have spawned — keep it alive past exit so the TV keeps playing (atexit would kill
        # it mid-cast). No-op when the engine wasn't used.
        engine.detach_spawned()
    # The advance decision is made HERE, once, for every delivery backend (ADR 0029). Deciding
    # it inside the backends is how the Tier-2 remux and the mirror ended up hardcoding False:
    # a binge on the TV died after one episode whenever the audio needed a remux. A backend
    # reports what it observed (pos/dur); only this layer knows whether an episode follows.
    advance = bool(next_label) and cast_delivery.is_finished(pos, dur)
    return CastOutcome(
        pos=pos, dur=dur, advance=advance, action=action, stream=chosen,
        started=delivery.started, cast_error=delivery.error,
        reencoded=reencoded, notice=notice,
        # After a failed remux the plan's track is NOT what plays (the DMR takes track 0,
        # possibly undecodable): report the language as unknown, never as verified.
        audio_lang=None if degraded_audio else plan.real_lang,
        audio_verified=plan.verified and not degraded_audio,
        safety_sub_lang=safety_sub_lang, sub_paths=sub_paths,
        sub_match=subs_pick.match, sub_offset=subs_pick.offset_s,
        subs_delivered=subs_delivered, audio_degraded=degraded_audio,
    )  # fmt: skip
