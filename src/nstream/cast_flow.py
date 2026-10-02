"""Shared cast decision tree: vet the audio plan, then mirror / Tier-2 remux / direct cast.

`run_cast` is the single body behind the interactive cast (`cli._play_on_cast`) and the
headless `--json --cast` branch (`headless_play.auto_play`), which used to carry two hand-synced
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

import os
import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace

from . import (
    api,
    cast_delivery,
    cast_vet,
    caster,
    engine,
    languages,
    log,
    mirror,
    notices,
    quality,
    remux,
    state,
    stream_select,
    subs,
    tracks,
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

    Remux and an undecodable track wait, and a direct plan whose container is not loadable
    (MKV rewrap, ADR 0022) — unless the live tier can run (ADR 0039/0041): then the TV
    starts in seconds and nothing waits."""
    if plan.mode == "remux" or plan.needs_remux:
        slow = True
    elif not plan.stream.get("url"):
        return False
    else:
        slow = not quality.container_castable(cast_vet.cast_container(cfg, plan.stream))
    if not slow or not plan.stream.get("url"):
        return slow
    size_gb = quality.parse_stream(plan.stream).size_gb
    return not remux.live_feasible(cfg, plan.stream["url"], size_gb)


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


# One worker: the subtitle fetch that overlaps a Tier-2 prepare (see `run_cast`).
_SUBS_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subs")

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
    start: float | None = None  # the position the delivery was LOADed at (see `_handoff_start`)
    delivery: str = ""  # "live" (HLS-TS) | "file" (complete remux) | "direct" | "mirror"
    sub_lang: str | None = None  # language of the delivered subtitle track


def prefetch_next(
    cfg: Config, typ: str, video_id: str, title: str, opts: PlayOpts, device: str
) -> None:
    """Binge prefetch (ADR 0039 follow-up): run the next episode's unattended selection —
    the same `prepare_stream` + cast vetting the real start will run — and, when it needs
    the live tier, start its producer ahead (`remux.prefetch_live`). The real start adopts
    it if it lands on the same release. Best effort: any failure only costs the speed-up."""
    try:
        results = api.streams(cfg, typ, video_id)
        runtime = api.expected_runtime_s(cfg, typ, video_id)
        vetted = stream_select.prepare_stream(
            cfg, results, opts, auto=True, reselect_on_wrong_audio=False, title=title,
            expected_runtime_s=runtime,
        )  # fmt: skip
        if vetted is None or not vetted.stream.get("url"):
            return
        chosen = vetted.stream
        exact = stream_select.exact_resolution(vetted.quality or 0)
        with stream_select.ranking_runtime(runtime):
            plan = cast_vet.vet_cast_audio(
                cfg, results, chosen, opts.audio_lang or cfg.primary,
                exact_resolution=exact, title=title, expected_s=runtime,
            )  # fmt: skip
        chosen = plan.stream
        needs = plan.mode == "remux" or plan.needs_remux or _needs_rewrap(cfg, chosen)
        if not needs or not chosen.get("url"):
            return
        remux.prefetch_live(
            cfg, chosen["url"], device=device, audio_index=plan.audio_index,
            source_key=stream_select.source_key(chosen),
            embedded=_embedded_for(cfg, opts, chosen, vetted.safety_sub_lang),
        )  # fmt: skip
    except Exception as e:  # noqa: BLE001 — a prefetch never disturbs the playing episode
        _log.info("prefetch episodio successivo fallito: %s", type(e).__name__)


def _needs_rewrap(cfg: Config, stream: Stream) -> bool:
    return not quality.container_castable(cast_vet.cast_container(cfg, stream))


def _embedded_for(
    cfg: Config, opts: PlayOpts, chosen: Stream, safety_sub_lang: str | None
) -> tuple[int, str] | None:
    """The embedded text subtitle to deliver as a live rendition (ADR 0042), or None: only
    when subtitles are wanted automatically (the safety net, or `--subs` without the
    interactive menu) and the release carries a full text track in a wanted language. The
    probe is the memoized one the vetting already ran."""
    if safety_sub_lang:
        langs = [safety_sub_lang]
    elif opts.sub_mode and opts.sub_mode != "menu":
        langs = [opts.sub_lang] if opts.sub_lang else list(cfg.subtitle_langs)
    else:
        return None
    return subs.embedded_pick(tracks.probe_tracks(chosen["url"]), langs)


def _handoff_start(cfg: Config, device: str, video_id: str, start: float | None) -> float | None:
    """The resume point at the moment of the LOAD, not at command start. Recasting the title
    the TV is already playing (another dub, a retry) used to restart from the position frozen
    when the command began — before a remux that can take minutes, while the old cast kept
    playing — and the merge into history was dropped by `clear_cast_session`. A live session
    for the same video on the same device is asked once; its position wins when it is later,
    and is merged into history like `--status` does."""
    session = state.cast_session_info()
    if not session or session.get("video_id") != video_id or session.get("device") != device:
        return start
    info = caster.status(device)
    pos = float(info.get("position") or 0.0)
    if pos <= (start or 0.0):
        return start
    state.update_from_receiver(
        cfg, device, pos, float(info.get("duration") or 0.0), title=info.get("title")
    )
    return pos


def _defer_to_instant(
    cfg: Config,
    results: list[Stream],
    opts: PlayOpts,
    plan: cast_vet.CastAudioPlan,
    target_lang: str,
    *,
    exact: int,
    expected_s: float,
    title: str = "",
) -> tuple[cast_vet.CastAudioPlan, str | None, bool]:
    """ADR 0035: a soft language preference must not force a full-file fetch when a verified
    direct cast exists at this quality. Explicit `--audio-lang` stays hard. Headless takes the
    direct cast and reports it; a TUI asks once before dropping the primary dub.

    Returns (plan, notice, safety_subs): `safety_subs` = the direct cast plays another dub, so
    target-language subtitles become the safety net."""
    if opts.audio_lang or not target_lang or not _cast_would_wait(cfg, plan):
        return plan, None, False
    instant = cast_vet.find_instant_direct(
        cfg,
        results,
        tuple(cfg.audio_langs),
        exact_resolution=exact,
        title=title,
        expected_s=expected_s,
    )
    if instant is None or instant.stream is plan.stream:
        return plan, None, False
    take = instant.real_lang == target_lang
    if not take:
        if opts.interactive and opts.confirm is not None:
            take = opts.confirm(
                f"«{target_lang}» solo dopo il download completo. "
                f"Parto subito in {instant.real_lang}?",
                True,
            )
        else:
            take = True
    if not take:
        return plan, None, False
    if instant.real_lang != target_lang:
        notice = cast_vet.instant_defer_notice(plan, instant, target_lang)
        notices.emit(f"{notice}")
        return instant, notice, True
    notices.emit("release diretta nella stessa lingua, salto il remux")
    return instant, None, False


@dataclass(frozen=True)
class _Settled:
    """The stream the delivery will use, and what it needs (see `_settle`)."""

    plan: cast_vet.CastAudioPlan
    chosen: Stream
    bad_video: str
    container: str
    needs_rewrap: bool
    needs_remux: bool
    refused_mirror: str  # the remux refusal the mirror stands in for ("" = none)
    notice: str | None  # set when a direct release replaced a refused remux
    # The complete-file refusal a live start bypasses (ADR 0039): if the live start then
    # fails, this is the honest `remux_infeasible` reason — never a mute direct cast.
    full_refusal: str = ""


def _settle(
    cfg: Config,
    results: list[Stream],
    opts: PlayOpts,
    plan: cast_vet.CastAudioPlan,
    chosen: Stream,
    bad_video: str,
    *,
    exact: int,
    expected_s: float,
    title: str = "",
) -> _Settled:
    """Settle the stream and decide what delivering it needs, feasibility included.

    The language reselect only offers video-castable candidates (its guard shares the video
    vetting), so a swap clears the bad-video verdict along with the stream. Once settled,
    every branch dereferences `chosen["url"]`: asserted once, honestly (ADR 0031 appendix —
    the KeyError: 'url' crash site). Remux is a codec decision orthogonal to language: an
    `absent` fallback whose default track is Dolby/DTS must be remuxed too (`needs_remux`),
    and a DMR-incompatible container on the SETTLED stream forces the Tier-2 rewrap to MP4
    (ADR 0022). Feasibility is decided BEFORE committing to a whole-file prepare (ADR 0036):
    the mirror stands in, else a verified direct release, else `CastRemuxInfeasible`."""
    if plan.stream is not chosen:
        bad_video = ""
    chosen = plan.stream
    if not chosen.get("url"):
        raise CastStreamUnresolved()
    container = cast_vet.cast_container(cfg, chosen)
    needs_rewrap = not quality.container_castable(container)
    needs_remux = plan.mode == "remux" or plan.needs_remux or needs_rewrap
    refused_mirror, notice, full_refusal = "", None, ""
    if needs_remux and not opts.mirror:
        size_gb = quality.parse_stream(chosen).size_gb
        why = remux.refusal(cfg, size_gb, interactive=opts.interactive)
        if why and remux.live_feasible(cfg, chosen["url"], size_gb):
            # Live needs only a window of the file on disk (ADR 0039): go ahead, and keep the
            # complete-file reason for the case the live start fails.
            full_refusal, why = why, None
        if why:
            mirror_ok_now = mirror.available()
            if opts.mirror is None and mirror_ok_now:
                refused_mirror = why
            else:
                langs = (opts.audio_lang,) if opts.audio_lang else tuple(cfg.audio_langs)
                alt = cast_vet.find_instant_direct(
                    cfg,
                    results,
                    langs,
                    exact_resolution=exact,
                    title=title,
                    expected_s=expected_s,
                )
                if alt is None or alt.stream is chosen:
                    if not mirror_ok_now and opts.mirror is None:
                        why = f"{why}; mirror non disponibile ({mirror.unavailable_reason()})"
                    raise CastRemuxInfeasible(why)
                notice = f"{why} → cast diretto {alt.real_lang or '?'}"
                notices.emit(f"{notice}")
                plan, chosen, bad_video = alt, alt.stream, ""
                if not chosen.get("url"):
                    raise CastStreamUnresolved()
                container = cast_vet.cast_container(cfg, chosen)
                needs_rewrap = needs_remux = False
    return _Settled(
        plan, chosen, bad_video, container, needs_rewrap, needs_remux, refused_mirror, notice,
        full_refusal,
    )  # fmt: skip


def _mirror_choice(
    cfg: Config,
    opts: PlayOpts,
    info: quality.StreamInfo,
    *,
    needs_remux: bool,
    bad_video: str,
    refused_mirror: str,
    available: Callable[[], bool],
) -> tuple[bool, str | None, bool]:
    """Whether to deliver through the mirror, with its notice and whether the notice is
    also a status line. Pure apart from the `available` probe (called lazily, only when a
    mirror is in play).

    The DMR plays AAC/HEVC/4K/HDR natively and instantly — strictly better than the mirror
    (1080p SDR re-encode, latency) — so mirroring only helps when the DMR can't take the file:
    - `bad_video` (ADR 0017) or a refused remux (ADR 0036): mpv decodes locally, the only way
      left to show the title;
    - an explicit `--mirror` (ADR 0023) forces it even for a decodable title;
    - with NO per-invocation preference (`opts.mirror is None`, ADR 0021), a remux that would
      be a pathological multi-GB fetch auto-switches to it (ADR 0015)."""
    force = bool(bad_video) or bool(refused_mirror)
    if not ((needs_remux or force or opts.mirror is True) and available()):
        return False, None, False
    auto = opts.mirror is None and _remux_is_pathological(info, cfg.cast_mirror_over_remux_gb)
    if not (opts.mirror is True or auto or force):
        return False, None, False
    if refused_mirror and not bad_video:
        return True, f"{refused_mirror} → mirror 1080p (decodifica locale)", False
    if force:
        return (
            True,
            f"video {bad_video.upper()} non decodificabile dal TV"
            " → mirror 1080p (decodifica locale)",
            False,
        )
    if auto:
        size = f" (~{info.size_gb:.0f} GB)" if info.size_gb else ""
        return (
            True,
            (
                f"remux troppo pesante{size} → mirror 1080p SDR "
                f"(soglia {cfg.cast_mirror_over_remux_gb} GB; avvio immediato)"
            ),
            True,
        )
    return True, None, False


def _with_container_mime(meta: caster.CastMeta | None, container: str) -> caster.CastMeta | None:
    """Declare the container's MIME on the LOAD instead of leaving the DMR to sniff (ADR
    0022): reaching a direct cast means the container is DMR-compatible (mp4/webm) or
    unknown; set contentType when known so the receiver doesn't guess."""
    mime = quality.container_mime(container)
    if mime and not (meta and meta.content_type):
        return replace(meta or caster.CastMeta(), content_type=mime)
    return meta


def _lang_switch(
    cfg: Config,
    results: list[Stream],
    opts: PlayOpts,
    *,
    exact: int,
    allowed: bool,
    title: str = "",
) -> tuple[tuple[str, ...], Callable[[str], str | None] | None, caster.ChooseLang | None]:
    """The in-cast audio switch ('a'): (dubs, resolver, menu). Only an interactive caller
    with a frontend menu pays the extra rank passes (ADR 0037); headless leaves it off."""
    if not allowed or opts.choose is None:
        return (), None, None
    menu = opts.choose

    def choose_lang(codes: tuple[str, ...]) -> str | None:
        return menu([(languages.name(c), c) for c in codes], "audio> ")

    cast_langs = cast_vet.cast_languages(cfg, results, exact_resolution=exact, title=title)
    if len(cast_langs) <= 1:
        return (), None, choose_lang
    return (
        cast_langs,
        cast_vet.cast_resolver(cfg, results, exact_resolution=exact, title=title),
        choose_lang,
    )


@log.phase("run_cast")
@stream_select.with_ranking_runtime
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
    prefetch_next: Callable[[], None] | None = None,
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
        cfg,
        results,
        chosen,
        exact_resolution=exact,
        title=title,
        expected_s=expected_runtime_s,
    )
    # --no-mirror is an explicit user intent: with undecodable video and the mirror
    # suppressed, fail explicitly rather than override the user (ADR 0021).
    if bad_video and (not mirror.available() or opts.mirror is False):
        raise CastVideoUnsupported(bad_video)
    target_lang = opts.audio_lang or cfg.primary
    # Container gate (ADR 0022): the DMR refuses .mkv on a direct cast though it decodes the
    # same HEVC/AAC in .mp4. Prefer an MP4 twin (a free direct cast) over a download+rewrap;
    # this is the optimization, the settled-stream check below is the guarantee.
    pre_container = chosen
    chosen, _bad_container = cast_vet.vet_cast_container(
        cfg,
        results,
        chosen,
        target_lang,
        exact_resolution=exact,
        title=title,
        expected_s=expected_runtime_s,
    )
    if chosen is not pre_container:
        bad_video = ""  # a vetted MP4 candidate supersedes the original's video verdict
    plan = cast_vet.vet_cast_audio(
        cfg,
        results,
        chosen,
        target_lang,
        exact_resolution=exact,
        title=title,
        expected_s=expected_runtime_s,
    )
    # ADR 0035: a soft language preference must not force a full-file fetch when a
    # verified direct cast exists at this quality. Explicit `--audio-lang` stays hard.
    # Headless takes the direct cast and reports it; a TUI asks once before dropping
    # the primary dub.
    plan, notice_defer, safety_from_defer = _defer_to_instant(
        cfg,
        results,
        opts,
        plan,
        target_lang,
        exact=exact,
        expected_s=expected_runtime_s,
        title=title,
    )
    if safety_from_defer and not opts.sub_lang:
        safety_sub_lang = target_lang
    st = _settle(
        cfg,
        results,
        opts,
        plan,
        chosen,
        bad_video,
        exact=exact,
        expected_s=expected_runtime_s,
        title=title,
    )
    plan, chosen, bad_video = st.plan, st.chosen, st.bad_video
    final_container, needs_rewrap, needs_remux = st.container, st.needs_rewrap, st.needs_remux
    refused_mirror = st.refused_mirror
    notice_defer = st.notice or notice_defer
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
        notices.emit(f"audio {target_lang} non disponibile{real}", code="audio_lang_absent")

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
    # A live Tier-2 (ADR 0039) starts in seconds with native video: the auto mirror only
    # stood in for a slow whole-file prepare, so it no longer applies when live can run.
    slow_prepare = needs_remux and not (
        not opts.mirror
        and remux.live_feasible(cfg, chosen["url"], quality.parse_stream(chosen).size_gb)
    )
    use_mirror, mirror_notice, loud = _mirror_choice(
        cfg, opts, quality.parse_stream(chosen),
        needs_remux=slow_prepare, bad_video=bad_video, refused_mirror=refused_mirror,
        available=mirror.available,
    )  # fmt: skip
    # Exactly one auto_subs call, with the effective safety language (normalization 2).
    # The resolved url/filename enable the exact-file hash match (ADR 0018). When the
    # delivery starts with a whole-file prepare, the subtitle fetch runs alongside it
    # instead of before it: its seconds hide behind the remux's minutes.
    pending_subs: Future[subs.SubsPick] | None = None
    embedded_used = False  # an embedded text track rides the live cast (ADR 0042)
    if needs_remux and not use_mirror:
        pending_subs = _SUBS_POOL.submit(
            subs.auto_subs, cfg, typ, video_id, work_dir, opts,
            safety_sub_lang=safety_sub_lang,
            video_url=chosen["url"], filename=subs.stream_filename(chosen),
        )  # fmt: skip
        subs_pick = subs.SubsPick()
    else:
        subs_pick = subs.auto_subs(
            cfg, typ, video_id, work_dir, opts, safety_sub_lang=safety_sub_lang,
            video_url=chosen["url"], filename=subs.stream_filename(chosen),
        )  # fmt: skip
        subs.report_safety_subs(subs_pick, safety_sub_lang)
    sub_paths = subs_pick.paths
    # Language of the fetched subtitle track (labels the side-loaded caption track on the TV).
    sub_lang = safety_sub_lang or opts.sub_lang or subs_pick.lang
    _log.info("cast '%s' → %s (%s/%s)", title, device, plan.mode, plan.real_lang or "?")
    if use_mirror:
        start = _handoff_start(cfg, device, video_id, start)
        if mirror_notice:
            notice = mirror_notice
            notices.emit(f"{notice}")
            if loud:
                ui.status(notice, kind="tv")
        delivery = mirror.cast_via_mirror(
            cfg, title, chosen["url"],
            device=device, start=start, sub_paths=sub_paths, follow=follow,
        )  # fmt: skip
        pos, dur = delivery.pos, delivery.dur
        subs_delivered = bool(sub_paths)  # mpv renders them into the mirrored frame
        action = "mirror"
        delivered_as = "mirror"
    else:
        if opts.mirror is True:
            # Reached the else with --mirror forced ⇒ `mirror.available()` is False (the only
            # way `mirror_ok` is False here): honour the intent with an honest fallback notice.
            print(MIRROR_UNAVAILABLE, file=sys.stderr)
        live_delivery = None
        embedded = _embedded_for(cfg, opts, chosen, safety_sub_lang) if needs_remux else None
        if needs_remux and remux.live_available(cfg):
            # ADR 0039: the TV starts on the first converted segments. ADR 0042: an embedded
            # text track in a wanted language rides as a rendition (the file's own track,
            # no download); otherwise the subtitle fetch is awaited (seconds) so the caption
            # track rides the LOAD.
            if embedded is None and pending_subs is not None:
                subs_pick = pending_subs.result()
                pending_subs = None
                subs.report_safety_subs(subs_pick, safety_sub_lang)
                sub_paths = subs_pick.paths
                sub_lang = safety_sub_lang or opts.sub_lang or subs_pick.lang
            start = _handoff_start(cfg, device, video_id, start)
            live_delivery = remux.cast_live(
                cfg, title, chosen["url"],
                device=device, audio_index=plan.audio_index, start=start,
                size_gb=quality.parse_stream(chosen).size_gb,
                sub_paths=sub_paths, sub_lang=sub_lang, embedded=embedded, follow=follow,
                meta=meta, on_event=on_event,
                source_key=stream_select.source_key(chosen), on_near_end=prefetch_next,
            )  # fmt: skip
            if live_delivery is not None and embedded is not None:
                subs_pick = subs.SubsPick(match="embedded", lang=embedded[1])
                sub_paths, sub_lang, pending_subs = (), embedded[1], None
                embedded_used = True
                notices.emit(f"sottotitoli {embedded[1]} dal file")
            if live_delivery is None and st.full_refusal:
                raise CastRemuxInfeasible(f"{st.full_refusal}; la diretta non è partita")
        if needs_remux and live_delivery is None:
            print(_prepare_line(chosen, plan, target_lang), file=sys.stderr)
        remux_path = (
            remux.remux_for_cast(
                chosen["url"],
                cfg,
                audio_index=plan.audio_index,
                size_gb=quality.parse_stream(chosen).size_gb,
                confirm=opts.confirm if opts.interactive else None,
                sub_index=embedded[0] if embedded else None,
            )  # fmt: skip
            if needs_remux and live_delivery is None
            else None
        )
        extracted = remux.embedded_vtt(remux_path) if remux_path and embedded else ""
        if embedded and extracted and os.path.isfile(extracted) and os.path.getsize(extracted):
            # The release's own track, extracted by the remux pass (ADR 0042): it replaces
            # the download, and the local alignment below still checks it.
            subs_pick = subs.SubsPick((extracted,), "embedded", lang=embedded[1])
            sub_paths, sub_lang, pending_subs = subs_pick.paths, subs_pick.lang, None
        if pending_subs is not None:  # fetched while the remux ran
            subs_pick = pending_subs.result()
            subs.report_safety_subs(subs_pick, safety_sub_lang)
            sub_paths = subs_pick.paths
            sub_lang = safety_sub_lang or opts.sub_lang or subs_pick.lang
        start = _handoff_start(cfg, device, video_id, start)
        if live_delivery is not None:
            delivery = live_delivery
            pos, dur, subs_delivered = delivery.pos, delivery.dur, delivery.subs_delivered
            reencoded = True
            action = "cast"
            delivered_as = "live"
        elif remux_path:
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
            delivered_as = "file"
        elif needs_rewrap and mirror.available():
            # ADR 0022 gap: the container rewrap is unavailable (cfg.cast_remux off or ffmpeg
            # missing) and the DMR refuses this .mkv LOAD — mirror it (mpv decodes any
            # container) instead of a silent black direct cast.
            notice = "rewrap non disponibile → mirror 1080p (il TV non carica questo container)"
            notices.emit(f"{notice}")
            delivery = mirror.cast_via_mirror(
                cfg, title, chosen["url"],
                device=device, start=start, sub_paths=sub_paths, follow=follow,
            )  # fmt: skip
            pos, dur = delivery.pos, delivery.dur
            subs_delivered = bool(sub_paths)
            action = "mirror"
            delivered_as = "mirror"
        else:
            if needs_remux:
                # remux failed mid-way (ffmpeg error, or a guard tripped after `refusal`)
                # → direct cast of a file whose first audio track is Dolby (silent on the
                # DMR) or the wrong dub. The planned track no longer describes what plays.
                notice = (
                    "remux non riuscito → cast diretto: l'audio potrebbe "
                    "risultare muto o in un'altra lingua"
                )
                notices.emit(notice, code="remux_failed", render=f"nstream: {ui.g().warn} {notice}")
                degraded_audio = True
            meta = _with_container_mime(meta, final_container)
            langs, resolver, choose_lang = _lang_switch(
                cfg, results, opts, exact=exact, allowed=allow_lang_switch, title=title
            )
            delivery = caster.cast(
                cfg, title, chosen["url"],
                device=device, start=start, sub_paths=sub_paths, sub_lang=sub_lang,
                langs=langs, resolve_lang=resolver, choose_lang=choose_lang, follow=follow,
                meta=meta, on_event=on_event,
            )  # fmt: skip
            pos, dur, subs_delivered = delivery.pos, delivery.dur, delivery.subs_delivered
            action = "cast"
            delivered_as = "direct"
    if (sub_paths or embedded_used) and not subs_delivered:
        # Honesty over silence: the subtitles were fetched but not attached to the cast
        # (e.g. WebVTT conversion/serving failed, or the mirror path with no burn-in).
        notices.emit(
            f"{ui.g().warn} sottotitoli scaricati ma non caricati sul TV",
            code="subs_not_delivered",
        )
    elif sub_paths or embedded_used:
        subs.report_unverified(subs_pick, hint="se sfasati: --sub-shift ±s")
    if delivery.started:
        # The new cast replaced the TV's content: a previous fire-and-return session no
        # longer describes it (the headless caller writes a fresh one right after). Cleared
        # only now — a cast that never started must not lose the old session's position.
        state.clear_cast_session()
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
        subs_delivered=subs_delivered, audio_degraded=degraded_audio, start=start,
        delivery=delivered_as, sub_lang=sub_lang if (sub_paths or embedded_used) else None,
    )  # fmt: skip
