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

from . import caster, engine, log, mirror, quality, remux, stream_select, subs
from .config import Config, PlayOpts, Stream

_log = log.get_logger("cast_flow")

# Single source for the `--mirror`-downgrade notice (normalization 3).
MIRROR_NOT_NEEDED = (
    "nstream: audio decodificabile dal TV → cast diretto nativo (mirror non necessario)"
)


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
    target_lang = opts.audio_lang or cfg.primary
    plan = stream_select.vet_cast_audio(cfg, results, chosen, target_lang)
    chosen = plan.stream
    # No dub carries the target language: cast the best pick anyway, with target-language
    # subtitles as a safety net (mirrors the local guard). Printed on both paths (norm. 1).
    if plan.mode == "absent" and target_lang:
        safety_sub_lang = target_lang
        print(
            f"nstream: audio {target_lang} non disponibile"
            + (f" (casto {plan.real_lang})" if plan.real_lang else "")
            + f"; sottotitoli {target_lang} attivati",
            file=sys.stderr,
        )
    # Exactly one auto_subs call, with the effective safety language (normalization 2).
    sub_paths = subs.auto_subs(cfg, typ, video_id, work_dir, opts, safety_sub_lang=safety_sub_lang)
    _log.info("cast '%s' → %s (%s/%s)", title, device, plan.mode, plan.real_lang or "?")

    notice: str | None = None
    reencoded = False
    # Backend strategy. The DMR plays AAC/HEVC/4K/HDR natively and instantly — strictly better
    # than the mirror (1080p SDR re-encode, latency) — so `--mirror` only actually mirrors when
    # the audio is one the DMR can't decode (plan.mode == "remux"): there mirroring (mpv decodes
    # Dolby/DTS locally → instant) beats the remux prepare-wait. For decodable audio, mirror is
    # transparently downgraded to the direct cast.
    if plan.mode == "remux" and opts.mirror and mirror.available():
        pos, dur, advance = mirror.cast_via_mirror(
            cfg, title, chosen["url"],
            device=device, start=start, sub_paths=sub_paths, follow=follow,
        )  # fmt: skip
        action = "mirror"
    else:
        if opts.mirror and plan.mode != "remux":
            print(MIRROR_NOT_NEEDED, file=sys.stderr)
        remux_path = (
            remux.remux_for_cast(
                chosen["url"],
                cfg,
                audio_index=plan.audio_index,
                size_gb=quality.parse_stream(chosen).size_gb,
            )  # fmt: skip
            if plan.mode == "remux"
            else None
        )
        if remux_path:
            pos, dur, advance = remux.cast_file(
                cfg, title, remux_path,
                device=device, start=start, sub_paths=sub_paths, follow=follow,
                meta=meta, on_event=on_event,
            )  # fmt: skip
            reencoded = True
        else:
            if plan.mode == "remux":
                # remux refused (size guard) or failed → direct cast of a file whose first
                # audio track is Dolby (silent on the DMR) or the wrong dub.
                notice = (
                    "remux non riuscito → cast diretto: l'audio potrebbe "
                    "risultare muto o in un'altra lingua"
                )
                print(f"nstream: ⚠ {notice}", file=sys.stderr)
            # In-cast audio switch ('a'): only the interactive path pays the extra rank
            # passes; headless callers leave allow_lang_switch False.
            langs: tuple[str, ...] = ()
            resolver = None
            if allow_lang_switch:
                cast_langs = stream_select.cast_languages(cfg, results)
                if len(cast_langs) > 1:
                    langs = cast_langs
                    resolver = stream_select.cast_resolver(cfg, results)
            pos, dur, advance = caster.cast(
                cfg, title, chosen["url"],
                device=device, start=start, sub_paths=sub_paths, next_label=next_label,
                langs=langs, resolve_lang=resolver, follow=follow,
                meta=meta, on_event=on_event,
            )  # fmt: skip
        action = "cast"
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
    )  # fmt: skip
