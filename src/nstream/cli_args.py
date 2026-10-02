"""CLI argument parser construction (kept separate from TUI flow in `cli`)."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Never

from . import __version__
from .api import CAT_MAP


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        if "--json" in sys.argv[1:]:
            # argparse messages can echo arbitrary user input, including credentials.
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "usage",
                        "message": "argomenti non validi; consulta --help",
                    }
                )
            )
            raise SystemExit(2)
        super().error(message)


# Flags that only the headless path implements. Using any without `--json` is a usage
# error (no silent no-op into the TUI home). `--audio-lang` is intentionally *not*
# here: it holds on TUI and headless alike.
_HEADLESS_ONLY: tuple[tuple[str, str], ...] = (
    ("year", "--year"),
    ("season", "--season"),
    ("episode", "--episode"),
    ("device", "--device"),
    ("probe", "--probe"),
    ("stop", "--stop"),
    ("status", "--status"),
    ("pause", "--pause"),
    ("resume", "--resume"),
    ("seek", "--seek"),
    ("sub_shift", "--sub-shift"),
    ("volume", "--volume"),
    ("follow", "--follow/--no-follow"),
)


def headless_only_misuse(args: argparse.Namespace) -> str | None:
    """If headless-only flags appear without `--json`, return a usage message; else None."""
    if getattr(args, "json", False):
        return None
    used: list[str] = []
    for attr, label in _HEADLESS_ONLY:
        val = getattr(args, attr, None)
        if attr in ("season", "episode", "seek", "sub_shift", "volume", "follow"):
            if val is not None:
                used.append(label)
        elif val:
            used.append(label)
    if not used:
        return None
    flags = ", ".join(used)
    return f"{flags} richiede --json"


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(
        prog="nstream",
        description=(
            "Native terminal-first Stremio-like client "
            "(Cinemeta + Torrentio/debrid or local P2P + mpv; cast via castbridge or catt)."
        ),
    )
    parser.add_argument("query", nargs="*", help="titolo da cercare (altrimenti chiede)")
    parser.add_argument(
        "--doctor", action="store_true", help="diagnostica locale senza rete o playback"
    )
    parser.add_argument(
        "--play",
        action="store_true",
        help="forza la riproduzione automatica (anche se disattivata)",
    )
    parser.add_argument(
        "--cast",
        action="store_true",
        help=(
            "manda lo stream a un Chromecast "
            "(castbridge se disponibile, altrimenti catt) invece di mpv"
        ),
    )
    parser.add_argument(
        "--mirror",
        action="store_true",
        help="cast via mirror nativo (mpv su output headless → sender): parte subito, 1080p SDR",
    )
    parser.add_argument(
        "--no-mirror",
        action="store_true",
        help="sopprimi il mirror per questa invocazione (anche l'auto-switch ADR-0015)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="forza la riproduzione locale in mpv (anche se il default è cast)",
    )
    parser.add_argument(
        "--subs", action="store_true", help="sottotitoli automatici nella lingua preferita"
    )
    parser.add_argument("--sub-menu", action="store_true", help="scegli i sottotitoli a mano (fzf)")
    parser.add_argument("--sub-lang", metavar="CODE", help="lingua sottotitoli da auto-scegliere")
    parser.add_argument(
        "--sub-offset", metavar="SEC", type=float, default=0.0,
        help="ritima i sottotitoli di ±SEC secondi (es. -2.5)",
    )  # fmt: skip
    parser.add_argument(
        "--sub-fps", metavar="SRC:DST",
        help="corregge il drift da framerate: fps per cui i sottotitoli sono stati creati"
        " : fps del video (es. 25:23.976)",
    )  # fmt: skip
    parser.add_argument(
        "--browse", nargs="?", const="popolari", choices=list(CAT_MAP),
        help="sfoglia un catalogo Cinemeta invece di cercare (default: popolari)",
    )  # fmt: skip
    typ_group = parser.add_mutually_exclusive_group()
    typ_group.add_argument(
        "--movies", action="store_true", help="solo film (ricerca, catalogo, cronologia)"
    )
    typ_group.add_argument(
        "--series", action="store_true", help="solo serie TV (ricerca, catalogo, cronologia)"
    )
    parser.add_argument(
        "-c", "--continue", dest="cont", action="store_true",
        help="riprendi dalla cronologia (continua a guardare)",
    )  # fmt: skip
    parser.add_argument("--no-history", action="store_true", help="non salvare la cronologia")
    parser.add_argument(
        "--no-autoplay", action="store_true", help="non proporre il prossimo episodio"
    )
    parser.add_argument("--settings", action="store_true", help="apri il menu impostazioni")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="spiega perché uno stream/audio verrebbe scelto (non riproduce)",
    )
    parser.add_argument(
        "--debrid-test",
        metavar="INFOHASH",
        help="diagnostica: prova cache+resolve del provider debrid nativo (aggiunge il torrent)",
    )
    parser.add_argument(
        "--forget-dead",
        action="store_true",
        help="svuota l'elenco delle sorgenti marcate come rimosse (non riproduce)",
    )
    parser.add_argument(
        "--forget-breakers",
        action="store_true",
        help="svuota i circuit breaker per-addon (ADR 0027; non riproduce)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="modalità headless non-interattiva: niente fzf, un oggetto JSON su stdout",
    )
    parser.add_argument(
        "--year",
        metavar="YYYY",
        help="disambigua il titolo per anno (richiede --json)",
    )
    parser.add_argument(
        "--season",
        type=int,
        metavar="N",
        help="stagione serie (richiede --json, default 1)",
    )
    parser.add_argument(
        "--episode",
        type=int,
        metavar="M",
        help="episodio serie (richiede --json, default 1)",
    )
    parser.add_argument(
        "--device",
        metavar="NAME",
        help="Chromecast di destinazione (richiede --json, evita il picker)",
    )
    parser.add_argument(
        "--audio-lang",
        metavar="CODE",
        help="forza la lingua audio/dub (es. eng, ita); fallisce se assente",
    )
    parser.add_argument(
        "--quality",
        metavar="RES",
        help=(
            "filtra per risoluzione esatta: auto, 720, 1080, 4k/2160 "
            "(TUI e --json; senza flag la TUI chiede, --json non filtra)"
        ),
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="elenca audio/sottotitoli disponibili (richiede --json; non riproduce)",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="ferma il cast in corso (richiede --json)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="stato del cast (player_state, titolo…; richiede --json)",
    )
    parser.add_argument(
        "--volume",
        type=int,
        metavar="N",
        help="volume Chromecast 0-100 (richiede --json; senza titolo = cast in corso)",
    )
    parser.add_argument(
        "--pause",
        action="store_true",
        help="mette in pausa il cast in corso (richiede --json)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="riprende il cast in pausa (richiede --json)",
    )
    parser.add_argument(
        "--seek",
        type=float,
        metavar="SEC",
        help="salta alla posizione SEC del cast in corso (richiede --json)",
    )
    parser.add_argument(
        "--sub-shift",
        type=float,
        metavar="SEC",
        help="sposta i sottotitoli del cast in diretta di SEC secondi (+ = dopo; richiede --json)",
    )
    parser.add_argument(
        "--follow",
        dest="follow",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="con --json+cast: segui fino a fine o ritorna subito (--no-follow, default)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="log verboso su stderr (oltre al file di log)"
    )
    parser.add_argument("--version", action="version", version=f"nstream {__version__}")
    return parser
