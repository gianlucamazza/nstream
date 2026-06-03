"""Native fzf settings menu: edit config.json and Stremio addons interactively.

Self-contained (does not import cli) so it can be reused from anywhere without a cycle.
The Real-Debrid token is read with getpass and never printed back.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import re
import shlex
import subprocess
import sys
import tempfile

from . import addons, config
from .config import Config

HWDEC_CHOICES = ["auto-safe", "auto", "vaapi", "nvdec", "vdpau", "no (disabilita)"]
MAXRES_CHOICES = [
    ("2160p (4K)", 2160),
    ("1080p (Full HD)", 1080),
    ("720p (HD)", 720),
    ("illimitata", 0),
]


def _fzf_select(
    rows: list[str], *, prompt: str, header: str = "", previews: list[str] | None = None
) -> int | None:
    """Show `rows` in fzf; return the selected index (input order) or None."""
    if not rows:
        return None
    lines = "".join(f"{i}\t{r}\n" for i, r in enumerate(rows))
    # No --height → full alternate screen (clean enter/exit, no scrollback buildup).
    args = [
        "fzf", "--prompt", prompt, "--with-nth", "2..", "--delimiter", "\t",
        "--no-sort", "--reverse", "--cycle",
    ]  # fmt: skip
    if header:
        args += ["--header", header]
    pv_path = None
    try:
        if previews:
            fd, pv_path = tempfile.mkstemp(prefix="nstream-help-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(p.replace("\n", " ") for p in previews))
            args += [
                "--preview", f'sed -n "$(({{n}}+1))p" {shlex.quote(pv_path)}',
                "--preview-window", "down:3:wrap",
            ]  # fmt: skip
        try:
            proc = subprocess.run(args, input=lines, capture_output=True, text=True)
        except FileNotFoundError:
            print("nstream: fzf non trovato", file=sys.stderr)
            return None
    finally:
        if pv_path:
            with contextlib.suppress(OSError):
                os.unlink(pv_path)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return int(proc.stdout.split("\t", 1)[0])


def _ask(prompt: str) -> str:
    """input() that treats EOF (Ctrl-D / closed stdin) as an empty answer,
    so it cancels the single action instead of crashing."""
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _token_status(cfg: Config) -> str:
    m = re.search(r"realdebrid=([^|]*)", cfg.torrentio_base)
    return "✓ impostato (••••)" if (m and m.group(1)) else "✗ assente"


def _with_token(base: str, token: str) -> str:
    if "realdebrid=" in base:
        return re.sub(r"realdebrid=[^|]*", f"realdebrid={token}", base)
    return f"sort=qualitysize|realdebrid={token}"


def _items(cfg: Config) -> list[tuple[str, str, str, str, str]]:
    """(key, label, kind, current_value, help) for each setting row."""
    return [
        (
            "audio_langs",
            "Lingue audio",
            "list",
            ",".join(cfg.audio_langs),
            "Preferenza audio mpv (--alang), in ordine. CSV, es: ita,eng,jpn",
        ),
        (
            "subtitle_langs",
            "Lingue sottotitoli",
            "list",
            ",".join(cfg.subtitle_langs),
            "Preferenza sottotitoli (--slang e --subs), in ordine. CSV, es: ita,eng",
        ),
        (
            "autoplay",
            "Autoplay prossimo ep.",
            "bool",
            "on" if cfg.autoplay else "off",
            "Overlay 'prossimo episodio' per le serie.",
        ),
        (
            "autoplay_lead",
            "Autoplay: anticipo",
            "int",
            f"{cfg.autoplay_lead}s",
            "Secondi prima della fine in cui appare l'overlay (1-120).",
        ),
        (
            "hwdec",
            "Accelerazione (hwdec)",
            "enum",
            cfg.hwdec or "off",
            "Decodifica hardware mpv (iniettata solo se non in mpv.conf). auto-safe consigliato.",
        ),
        (
            "history_enabled",
            "Cronologia / resume",
            "bool",
            "on" if cfg.history_enabled else "off",
            "Salva la posizione per resume e continua-a-guardare.",
        ),
        (
            "mpv_quiet",
            "Output mpv pulito",
            "bool",
            "on" if cfg.mpv_quiet else "off",
            "Nasconde track-list e warning mpv; tiene barra e errori.",
        ),
        (
            "hw_filter",
            "Filtro hardware stream",
            "bool",
            "on" if cfg.hw_filter else "off",
            "Auto-sceglie il miglior stream giocabile; 8K/DV P5/non-HW in fondo (⚠).",
        ),
        (
            "max_resolution",
            "Risoluzione massima",
            "maxres",
            f"{cfg.max_resolution}p" if cfg.max_resolution else "illimitata",
            "Esclude gli stream oltre questa risoluzione (es. 8K). 'illimitata' = nessun limite.",
        ),
        (
            "allow_software",
            "Consenti codec non-HW",
            "bool",
            "on" if cfg.allow_software else "off",
            "Tiene anche i codec senza decodifica hardware (es. AV1 su GPU che non lo supporta).",
        ),
        (
            "allow_dv5",
            "Consenti Dolby Vision P5",
            "bool",
            "on" if cfg.allow_dv5 else "off",
            "Tiene gli stream DV Profile 5 (si vedono male senza display/processing DV).",
        ),
        (
            "torrentio_base",
            "Token Real-Debrid",
            "token",
            _token_status(cfg),
            "Token RD nel config (chmod 600, mai loggato). Input mascherato.",
        ),
        (
            "__addons__",
            "Plugin / Addon Stremio",
            "submenu",
            f"{len(cfg.addons)} extra",
            "Aggiungi/rimuovi addon compatibili Stremio (stream, sottotitoli, cataloghi).",
        ),
    ]


def run_settings(cfg: Config | None = None) -> None:
    """Interactive settings loop. Persists each change immediately (atomic, 0600)."""
    if cfg is None:
        cfg = config.load()
    while True:
        items = _items(cfg)
        rows = [f"⚙ {label:24s} {value}" for (_, label, _, value, _) in items]
        previews = [help for (*_, help) in items]
        idx = _fzf_select(
            rows, prompt="impostazioni> ", header="INVIO: modifica · ESC: esci", previews=previews
        )
        if idx is None:
            return
        key, label, kind, _, _ = items[idx]
        try:
            _edit(cfg, key, kind, label)
        except (EOFError, KeyboardInterrupt):
            return
        cfg = config.load()


def _edit(cfg: Config, key: str, kind: str, label: str) -> None:
    if kind == "submenu":
        _addons_menu(cfg)
    elif kind == "bool":
        config.save({key: not getattr(cfg, key)})
    elif kind == "int":
        raw = _ask(f"{label} (1-120): ")
        if raw:
            try:
                config.save({key: max(1, min(int(raw), 120))})
            except ValueError:
                print("nstream: valore non valido", file=sys.stderr)
    elif kind == "list":
        raw = _ask(f"{label} (CSV, es. ita,eng): ")
        if raw:
            config.save({key: [x.strip() for x in raw.split(",") if x.strip()]})
    elif kind == "enum":  # hwdec
        i = _fzf_select(HWDEC_CHOICES, prompt=f"{label}> ")
        if i is not None:
            config.save({key: "" if HWDEC_CHOICES[i].startswith("no") else HWDEC_CHOICES[i]})
    elif kind == "maxres":
        labels = [c[0] for c in MAXRES_CHOICES]
        i = _fzf_select(labels, prompt=f"{label}> ")
        if i is not None:
            config.save({key: MAXRES_CHOICES[i][1]})
    elif kind == "token":
        token = getpass.getpass("Token Real-Debrid (nascosto): ").strip()
        if token:
            config.save({"torrentio_base": _with_token(cfg.torrentio_base, token)})
            print("nstream: token aggiornato", file=sys.stderr)


# --- addons submenu ------------------------------------------------------


def _addons_menu(cfg: Config) -> None:
    while True:
        eff = addons.effective_addons(cfg)
        rows: list[str] = []
        for a in eff:
            tag = "[built-in]" if a.builtin else "[extra]   "
            rows.append(f"{tag} {a.name:18s} {','.join(a.resources)}")
        rows.append("➕ Aggiungi addon…")
        rows.append("⬅  Indietro")
        idx = _fzf_select(
            rows, prompt="plugin> ", header="INVIO: aggiungi / rimuovi (solo extra) · ESC: indietro"
        )
        if idx is None or idx == len(rows) - 1:
            return
        if idx == len(rows) - 2:
            _add_addon(cfg)
            cfg = config.load()
            continue
        addon = eff[idx]
        if addon.builtin:
            print("nstream: addon built-in, non rimovibile", file=sys.stderr)
            continue
        if _ask(f"Rimuovere '{addon.name}'? [y/N] ").lower() == "y":
            remaining = [u for u in cfg.addons if u != addon.manifest_url]
            config.save({"addons": remaining})
            cfg = config.load()


def _add_addon(cfg: Config) -> None:
    url = _ask("URL manifest addon (…/manifest.json): ")
    if not url:
        return
    if not url.endswith("manifest.json"):
        print("nstream: l'URL deve terminare con manifest.json", file=sys.stderr)
        return
    if url in cfg.addons:
        print("nstream: addon già presente", file=sys.stderr)
        return
    addon = addons.load_addon(url, use_cache=False)
    if addon is None:
        print("nstream: manifest non raggiungibile o non valido", file=sys.stderr)
        return
    config.save({"addons": [*cfg.addons, url]})
    print(f"nstream: aggiunto '{addon.name}' ({','.join(addon.resources)})", file=sys.stderr)


def onboard() -> None:
    """First-run: prompt for the Real-Debrid token and write a minimal config."""
    print("Primo avvio nstream — configura il token Real-Debrid.", file=sys.stderr)
    print("Ottienilo su https://real-debrid.com/apitoken", file=sys.stderr)
    try:
        token = getpass.getpass("Token Real-Debrid (nascosto): ").strip()
    except EOFError:
        token = ""
    if not token:
        raise config.ConfigError("token non fornito")
    config.save({"torrentio_base": f"sort=qualitysize|realdebrid={token}"})
