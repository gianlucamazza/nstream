"""One rendering of domain failures for every frontend (ADR 0037).

The TUI (`cli`) and `--json` (`headless`, `headless_play`) used to translate the same
exceptions four times, and the wording drifted. `describe` maps an exception to a
`Failure`: a stable `code` (the documented `--json` error), a user `message` (also the TUI
header notice), an optional `hint` naming CLI flags (JSON only — the TUI has no flags), and
extra JSON `fields`. Orchestration tier: imported by frontends only, never by the domain.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from . import api, cast_flow, stream_select
from .caster import CastUnavailable


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    hint: str = ""
    fields: dict[str, object] = field(default_factory=dict)

    def payload(self) -> dict:
        """The `--json` error object."""
        message = f"{self.message}; {self.hint}" if self.hint else self.message
        return {"ok": False, "error": self.code, "message": message, **self.fields}


# Exceptions `describe` knows; frontends catch exactly these.
KNOWN: tuple[type[BaseException], ...] = (
    api.IdUntranslated,
    stream_select.QualityUnavailable,
    stream_select.AudioLangUnavailable,
    stream_select.ContentTooShort,
    cast_flow.CastStreamUnresolved,
    cast_flow.CastRemuxInfeasible,
    cast_flow.CastVideoUnsupported,
    CastUnavailable,
)


def describe(
    exc: BaseException,
    *,
    title: str = "",
    available_audio: Sequence[str] = (),
    available_resolutions: Sequence[int] = (),
) -> Failure:
    """The `Failure` for one of `KNOWN`. `available_*` fill the lists when the exception
    doesn't carry its own."""
    of = f" per «{title}»" if title else ""
    if isinstance(exc, api.IdUntranslated):
        raw = exc.video_id or "—"
        return Failure(
            "id_untranslated",
            f"nessun id IMDb{of} per il catalogo {raw} — lo stream richiede un tt",
            fields={"catalog_id": raw},
        )
    if isinstance(exc, stream_select.QualityUnavailable):
        have = list(exc.available) or list(available_resolutions)
        return Failure(
            "quality_unavailable",
            f"nessuno stream {exc.quality}p riproducibile{of}"
            f" (disponibili: {', '.join(f'{r}p' for r in have) or '—'})",
            fields={"available_resolutions": have},
        )
    if isinstance(exc, stream_select.AudioLangUnavailable):
        have = list(exc.available) or list(available_audio)
        where = f" dalle tracce reali di «{title}»" if title else " dalle tracce reali"
        return Failure(
            "audio_lang_unavailable",
            (f"audio «{exc.lang}» assente{where}" if exc.real_tracks
             else f"audio «{exc.lang}» non disponibile{of}")
            + f" (disponibili: {', '.join(have) or '—'})",
            fields={"available_audio": have},
        )  # fmt: skip
    if isinstance(exc, stream_select.ContentTooShort):
        v = exc.verdict
        return Failure(
            "sources_truncated",
            f"le sorgenti{of} contengono un file troppo corto ({v.reason}): "
            "placeholder o sample, non il video",
            hint="prova un'altra qualità (--quality)",
            fields={
                "duration_s": round(v.duration, 1) or None,
                "expected_runtime_s": round(v.expected, 1) or None,
                "truncated_sources": exc.count,
            },
        )
    if isinstance(exc, cast_flow.CastStreamUnresolved):
        # Retry-worthy, unlike the "proven gone" codes (ADR 0031 appendix).
        return Failure(
            "no_playable_stream",
            "nessuna sorgente castabile risolvibile ora (swarm senza peer, o file non ancora "
            "trasferito dal debrid) — riprova più tardi o scegli un'altra release",
            hint="o prova --local / un'altra qualità (--quality)",
        )
    if isinstance(exc, cast_flow.CastRemuxInfeasible):
        return Failure(
            "remux_infeasible",
            f"l'audio di questa release va convertito ma {exc.reason}",
            hint="libera spazio, prova --quality 1080 o --local",
            fields={"reason": exc.reason},
        )
    if isinstance(exc, cast_flow.CastVideoUnsupported):
        return Failure(
            "video_codec_unsupported",
            f"video {exc.codec.upper()} non decodificabile dal Chromecast e nessuna release "
            "alternativa castabile — riproduci in locale o scegli un'altra release",
            hint="riprova con --local o con un'altra qualità (--quality)",
            fields={"video_codec": exc.codec},
        )
    if isinstance(exc, CastUnavailable):
        return Failure("device_not_found", str(exc))
    raise TypeError(f"failures.describe: {type(exc).__name__} non mappata")
