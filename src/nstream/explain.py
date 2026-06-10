"""Diagnostic renderer for the stream/audio auto-selection — the `--explain` command.

Answers "why was this file/audio picked?" by reconstructing the exact decision with the
same primitives the player uses (`quality.rank_streams`, `quality.score_components`,
`player._lang_defaults`, `tracks.probe_tracks`) and printing it as a plain, greppable
table: detected Caps + FilterSpec, every stream with its score terms and playable/excluded
status, the auto-pick, and the audio tracks mpv would choose. Read-only: it never plays.
"""

from __future__ import annotations

from . import languages, player, quality, tracks, ui
from .config import Config, Stream


def _rank(
    cfg: Config, results: list[Stream], *, cast: bool
) -> tuple[
    list[quality.RankedStream], list[quality.RankedStream], quality.Caps, quality.FilterSpec
]:
    caps = quality.cast_caps() if cast else quality.detect_caps()
    spec = quality.FilterSpec.from_config(cfg, cast_audio=cast)
    playable, excluded = quality.rank_streams(results, caps, spec)
    return playable, excluded, caps, spec


def auto_pick(cfg: Config, results: list[Stream], *, cast: bool) -> quality.RankedStream | None:
    """The stream nstream would auto-play for this profile, or None if nothing qualifies."""
    playable, _, _, _ = _rank(cfg, results, cast=cast)
    return playable[0] if playable else None


_COMP_LABEL = {
    "cached": "cached",
    "resolution": "res",
    "cast_audio": "caud",
    "cast_h264": "h264",
    "lang": "lang",
    "source": "src",
    "hevc": "hevc",
    "seeders": "seed",
}


def _fmt_components(comp: dict[str, float | int | bool]) -> str:
    """Compact one-line render of the score terms (precedence order). Profile-agnostic:
    renders whatever keys the dict carries (local has `hevc`, cast has `cast_audio`/
    `cast_h264`), so it works for both score profiles."""
    parts = []
    for key, val in comp.items():
        if key == "size":
            parts.append(f"sz={-float(val):.1f}")  # stored negated; show the real size
        elif isinstance(val, bool):
            parts.append(f"{_COMP_LABEL.get(key, key)}={int(val)}")
        else:
            parts.append(f"{_COMP_LABEL.get(key, key)}={val}")
    return " ".join(parts)


def _fmt_info(info: quality.StreamInfo) -> str:
    langs = ",".join(sorted(info.languages)) if info.languages else "untagged"
    tags = [
        f"{info.resolution}p" if info.resolution else "?p",
        info.codec or "?",
        info.source or "?src",
        info.audio or "?aud",
        langs,
    ]
    return " ".join(tags)


def _row(
    idx: int,
    r: quality.RankedStream,
    audio_langs: tuple[str, ...],
    mark: str,
    *,
    cast: bool = False,
) -> str:
    comp = quality.score_components(r.info, audio_langs, cast=cast)
    name = (r.stream.get("title") or "").split("\n", 1)[0][:60]
    return f"{idx:>2} {mark:<7} {_fmt_info(r.info):<46}  [{_fmt_components(comp)}]  {name}"


def explain_streams(cfg: Config, results: list[Stream], *, cast: bool) -> str:
    """Render the full ranking decision for one profile (local GPU or Chromecast)."""
    if not results:
        return "nessuno stream restituito da Torrentio."
    playable, excluded, caps, spec = _rank(cfg, results, cast=cast)
    profile = "CAST (Chromecast)" if cast else "LOCALE (mpv/GPU)"
    lines = [
        f"=== profilo {profile} ===",
        f"Caps: codecs={','.join(sorted(caps.codecs))} max_res={caps.max_resolution} "
        f"vaapi={caps.vaapi}",
        f"Filtri: max_res={spec.max_resolution} lang_filter={spec.lang_filter} "
        f"audio_langs={','.join(spec.audio_langs) or '—'} exclude_camrip={spec.exclude_camrip} "
        f"min_seeders={spec.min_seeders} dedup={spec.dedup} cast_audio={spec.cast_audio} "
        f"allow_sw={spec.allow_software} allow_dv5={spec.allow_dv5}",
        f"Risultati: {len(results)} totali → {len(playable)} giocabili, {len(excluded)} esclusi"
        + (f", {len(results) - len(playable) - len(excluded)} doppioni" if spec.dedup else ""),
        "",
        "PLAYABLE (ordine di score, best-first):",
    ]
    for i, r in enumerate(playable):
        mark = f"{ui.g().cached}PICK" if i == 0 else ""
        lines.append(_row(i + 1, r, spec.audio_langs, mark, cast=cast))
    if excluded:
        lines.append("")
        lines.append("ESCLUSI (motivo):")
        for i, r in enumerate(excluded):
            lines.append(_row(i + 1, r, spec.audio_langs, ui.g().warn, cast=cast))
            lines[-1] = lines[-1].replace("[", f"[escluso: {r.reason}] [", 1)
    return "\n".join(lines)


def explain_audio(cfg: Config, pick: quality.RankedStream | None) -> str:
    """Explain which audio track the auto-picked file would play locally: the --alang
    order nstream injects, plus the real ffprobe tracks (best-effort) with the one mpv
    would select highlighted. This is where 'why this audio' is answered."""
    lines = ["=== audio (auto-pick locale) ==="]
    if pick is None:
        return "\n".join([*lines, "nessuno stream giocabile → niente da analizzare."])
    alang = [a for a in player._lang_defaults(cfg) if a.startswith("--alang")]
    lines.append(f"Preferenza: {alang[0] if alang else '--alang non iniettato (override utente)'}")
    lines.append(f"Stream taggato: {_fmt_info(pick.info)}")
    url = pick.stream.get("url") or ""
    tr = tracks.probe_tracks(url) if url else tracks.Tracks()
    if tr.empty():
        lines.append("Tracce reali: ffprobe non disponibile o probe fallito → mpv usa i default.")
        return "\n".join(lines)
    chosen = _mpv_audio_choice(tr.audio, cfg.audio_langs)
    lines.append("Tracce audio reali nel file (ffprobe):")
    for t in tr.audio:
        mark = "  → mpv sceglie" if chosen is not None and t.id == chosen.id else ""
        ch = f" {t.channels}ch" if t.channels else ""
        title = f' "{t.title}"' if t.title else ""
        lines.append(f"  aid={t.id} {t.lang or 'und'} {t.codec}{ch}{title}{mark}")
    primary = cfg.primary
    real = {code for t in tr.audio if (code := languages.track_lang(t.lang, t.title))}
    if primary and real and primary not in real:
        lines.append(
            f"Guardia lingua: nessuna traccia {primary} nel pick → in riproduzione nstream "
            f"cercherebbe una sorgente taggata {primary} (riselezione), altrimenti "
            f"attiverebbe i sottotitoli {primary} di sicurezza."
        )
    if chosen is None:
        lines.append(
            "Nessuna traccia nelle lingue preferite → mpv ripiega sulla default del file "
            "(possibile causa di 'audio sbagliato'): scegli un altro file o usa il menu tracce."
        )
    return "\n".join(lines)


def _mpv_audio_choice(audio: list[tracks.Track], audio_langs: list[str]) -> tracks.Track | None:
    """The track mpv's --alang would pick: first track in the first preferred language
    that's present. None when no preferred language matches (mpv then uses its default).
    Tags are normalised so a 2-letter container tag (`it`) matches a 3-letter pref (`ita`)."""
    for lang in audio_langs:
        want = languages.normalize(lang)
        for t in audio:
            if languages.normalize(t.lang) == want:
                return t
    return None
