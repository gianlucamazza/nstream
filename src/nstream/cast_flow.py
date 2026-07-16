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
  3. the "mirror not needed → direct cast" message is a single constant (the two copies
     had drifted by one word).

What stays in the callers: device resolution, the headless volume guard (gated on
`CastOutcome.action == "cast"`), and the JSON protocol/accounting. The per-cast
`_log.info` line moved here, so the headless path now logs the cast decision too.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import caster, engine, log, mirror, quality, remux, state, stream_select, subs, ui
from .config import Config, PlayOpts, Stream

_log = log.get_logger("cast_flow")

# Single source for the `--mirror`-downgrade notice (normalization 3).
MIRROR_NOT_NEEDED = (
    "nstream: audio decodificabile dal TV → cast diretto nativo (mirror non necessario)"
)


class CastVideoUnsupported(Exception):
    """No cast candidate carries video the DMR can render, and the mirror fallback is not
    available (ADR 0017). Casting anyway would play black with state PLAYING and no receiver
    error — the callers surface this instead (headless: `video_codec_unsupported`)."""

    def __init__(self, codec: str):
        self.codec = codec
        super().__init__(
            f"video {codec} non decodificabile dal Chromecast e nessuna release alternativa"
        )


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
) -> CastOutcome:
    """Cast `chosen` to `device` in the target audio language. The Default Media Receiver
    plays a file's first audio track and can't switch tracks, so the language is enforced
    at selection time (`vet_cast_audio`): cast directly when the first track is already
    target + decodable, remux to select the track otherwise, or reselect a dub that has it;
    no dub at all → cast the best pick with target-language safety subtitles.

    `allow_lang_switch` wires the interactive in-cast audio switch ('a': langs + resolver,
    an extra rank pass) — leave it False on headless paths. `follow=False` (headless
    fire-and-return) also detaches a TorrServer the engine may have spawned, so the TV
    keeps streaming past process exit."""
    # Video first (ADR 0017): a video codec the DMR can't render casts as PLAYING + black
    # screen with no receiver error, so the REAL codec is verified before any side effect.
    # No castable candidate and no mirror to decode locally → explicit failure, not a black cast.
    chosen, bad_video = stream_select.vet_cast_video(cfg, results, chosen)
    if bad_video and not mirror.available():
        raise CastVideoUnsupported(bad_video)
    # A new cast replaces the TV's content: a previous fire-and-return session no longer
    # describes it (the headless caller re-writes a fresh one right after this returns).
    state.clear_cast_session()
    target_lang = opts.audio_lang or cfg.primary
    plan = stream_select.vet_cast_audio(cfg, results, chosen, target_lang)
    if plan.stream is not chosen:
        # The language reselect only offers video-castable candidates (its guard shares
        # this vetting), so a swap clears the bad-video verdict along with the stream.
        bad_video = ""
    chosen = plan.stream
    # Remux is a codec decision, orthogonal to language availability: `remux` mode selects a
    # target-language track, but an `absent` fallback whose default track is Dolby/DTS must be
    # remuxed too, or it casts silent (the DMR can't decode it). `plan.needs_remux` carries that.
    needs_remux = plan.mode == "remux" or plan.needs_remux
    # No dub carries the target language: cast the best pick anyway, with target-language
    # subtitles as a safety net (mirrors the local guard). Printed on both paths (norm. 1).
    # An EXPLICIT `--sub-lang` wins over this primary-language safety default: the user asked
    # for a specific subtitle language, so honour it (still warn that the audio isn't primary).
    if plan.mode == "absent" and target_lang:
        real = f" (casto {plan.real_lang})" if plan.real_lang else ""
        if opts.sub_lang:
            print(f"nstream: audio {target_lang} non disponibile{real}", file=sys.stderr)
        else:
            safety_sub_lang = target_lang
            print(
                f"nstream: audio {target_lang} non disponibile{real}"
                f"; sottotitoli {target_lang} attivati",
                file=sys.stderr,
            )
    # Exactly one auto_subs call, with the effective safety language (normalization 2).
    # The resolved url/filename enable the exact-file hash match (ADR 0018).
    subs_pick = subs.auto_subs(
        cfg, typ, video_id, work_dir, opts, safety_sub_lang=safety_sub_lang,
        video_url=chosen.get("url"), filename=subs.stream_filename(chosen),
    )  # fmt: skip
    sub_paths = subs_pick.paths
    # Language of the fetched subtitle track (labels the side-loaded caption track on the TV).
    sub_lang = safety_sub_lang or opts.sub_lang
    _log.info("cast '%s' → %s (%s/%s)", title, device, plan.mode, plan.real_lang or "?")

    notice: str | None = None
    reencoded = False
    # Backend strategy. The DMR plays AAC/HEVC/4K/HDR natively and instantly — strictly better
    # than the mirror (1080p SDR re-encode, latency) — so mirroring only helps when the audio is
    # one the DMR can't decode (`needs_remux`): there mpv decodes Dolby/DTS locally and starts
    # instantly, beating the remux prepare-wait. It is chosen when EITHER the user forced it
    # (`--mirror`/`cast_mode`), OR the remux would be a pathological multi-GB fetch (a 4K
    # Dolby-only pick) and the mirror is available (ADR 0015): instant start + no download beats
    # a 30-60 GB fetch, at the cost of 1080p SDR. Below the size threshold the remux still wins
    # (native video/HDR). For decodable audio, `--mirror` is transparently downgraded to a
    # direct cast.
    info = quality.parse_stream(chosen)
    # `bad_video` forces the mirror regardless of audio: mpv decodes the legacy video
    # locally, the only remaining way to show this title on the TV (availability was
    # already vetted above — reaching here with `bad_video` implies mirror.available()).
    force_mirror = bool(bad_video)
    mirror_ok = (needs_remux or force_mirror) and mirror.available()
    auto_mirror = (
        mirror_ok
        and not opts.mirror
        and _remux_is_pathological(info, cfg.cast_mirror_over_remux_gb)
    )
    if mirror_ok and (opts.mirror or auto_mirror or force_mirror):
        if force_mirror:
            notice = (
                f"video {bad_video.upper()} non decodificabile dal TV"
                " → mirror 1080p (decodifica locale)"
            )
            print(f"nstream: {notice}", file=sys.stderr)
        elif auto_mirror:
            size = f" (~{info.size_gb:.0f} GB)" if info.size_gb else ""
            notice = (
                f"remux 4K troppo pesante{size} → mirror 1080p (avvio immediato, senza download)"
            )
            print(f"nstream: {notice}", file=sys.stderr)
        pos, dur, advance = mirror.cast_via_mirror(
            cfg, title, chosen["url"],
            device=device, start=start, sub_paths=sub_paths, follow=follow,
        )  # fmt: skip
        subs_delivered = bool(sub_paths)  # mpv renders them into the mirrored frame
        action = "mirror"
    else:
        if opts.mirror and not needs_remux:
            print(MIRROR_NOT_NEEDED, file=sys.stderr)
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
            pos, dur, advance, subs_delivered = remux.cast_file(
                cfg, title, remux_path,
                device=device, start=start, sub_paths=sub_paths, sub_lang=sub_lang, follow=follow,
                meta=meta, on_event=on_event,
            )  # fmt: skip
            reencoded = True
        else:
            if needs_remux:
                # remux refused (size guard) or failed → direct cast of a file whose first
                # audio track is Dolby (silent on the DMR) or the wrong dub.
                notice = (
                    "remux non riuscito → cast diretto: l'audio potrebbe "
                    "risultare muto o in un'altra lingua"
                )
                print(f"nstream: {ui.g().warn} {notice}", file=sys.stderr)
            # In-cast audio switch ('a'): only the interactive path pays the extra rank
            # passes; headless callers leave allow_lang_switch False.
            langs: tuple[str, ...] = ()
            resolver = None
            if allow_lang_switch:
                cast_langs = stream_select.cast_languages(cfg, results)
                if len(cast_langs) > 1:
                    langs = cast_langs
                    resolver = stream_select.cast_resolver(cfg, results)
            pos, dur, advance, subs_delivered = caster.cast(
                cfg, title, chosen["url"],
                device=device, start=start, sub_paths=sub_paths, sub_lang=sub_lang,
                next_label=next_label, langs=langs, resolve_lang=resolver, follow=follow,
                meta=meta, on_event=on_event,
            )  # fmt: skip
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
    return CastOutcome(
        pos=pos, dur=dur, advance=advance, action=action, stream=chosen,
        reencoded=reencoded, notice=notice,
        audio_lang=plan.real_lang, audio_verified=plan.verified,
        safety_sub_lang=safety_sub_lang, sub_paths=sub_paths,
        sub_match=subs_pick.match, sub_offset=subs_pick.offset_s,
        subs_delivered=subs_delivered,
    )  # fmt: skip
