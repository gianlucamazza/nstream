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
import sys
import tempfile

from . import addons, config, debrid, engine, languages, picker, ui, util
from .config import Config

HWDEC_CHOICES = ["auto-safe", "auto", "vaapi", "nvdec", "vdpau", "no (disabilita)"]
MAXRES_CHOICES = [
    ("2160p (4K)", 2160),
    ("1080p (Full HD)", 1080),
    ("720p (HD)", 720),
    ("illimitata", 0),
]
# Fixed value lists for the "choice" setting kind.
CHOICE_VALUES: dict[str, list[str]] = {
    "nerd_font": ["auto", "on", "off"],
    "image_mode": ["auto", "off"],
}

# `catt scan` text line: "192.0.2.10 - 43PUS9235/12 - Philips TPM191E". We parse
# IP + name from text because `catt scan -j` is broken in current catt (CastInfo has
# no _asdict). Shared by the settings device picker and cli's on-cast resolver.
_SCAN_RE = re.compile(r"^([\d.]+) - (.+?) - ")


def scan_devices() -> list[tuple[str, str]]:
    """Discover Chromecasts on the LAN via `catt scan` as (name, ip) pairs (deduped by
    name, stable order). The IP lets callers cast with `catt -d <ip>`, which is robust
    to mDNS name-resolution flakiness (e.g. right after a network change). Best-effort:
    returns [] if catt is missing or the scan fails."""
    print("🔍 cerco Chromecast…", file=sys.stderr)
    proc = util.run_cmd(["catt", "scan"], timeout=util.CATT_SCAN_TIMEOUT)
    if proc is None:
        return []
    devices: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        m = _SCAN_RE.match(line.strip())
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            devices.append((m.group(2), m.group(1)))  # (name, ip)
    return devices


def _fzf_select(
    rows: list[str], *, prompt: str, header: str = "", previews: list[str] | None = None
) -> int | None:
    """Show `rows` in fzf; return the selected index (input order) or None."""
    if not rows:
        return None
    lines = "".join(f"{i}\t{r}\n" for i, r in enumerate(rows))
    # No --height → full alternate screen (clean enter/exit, no scrollback buildup).
    caps = ui.active_caps()
    args = [
        "fzf", "--prompt", prompt, "--with-nth", "2..", "--delimiter", "\t",
        "--no-sort", "--reverse", "--cycle",
        "--ansi", "--border", "rounded", "--info", "inline",
        "--pointer", ui.glyphs(caps).play, *ui.fzf_color_arg(caps),
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
        proc = util.run_cmd(args, input=lines)
        if proc is None:
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


def _pick_languages(current: list[str], label: str) -> list[str] | None:
    """Multi-select the preferred languages from the registry. Currently-selected codes
    are listed first (in their existing order) with a check glyph, the rest follow in
    registry order; the returned list preserves that display order so `--alang` priority
    is kept. None/empty when nothing was marked."""
    g = ui.glyphs(ui.active_caps())
    chosen = [c for c in current if c in languages.by_code]
    rest = [lang.code for lang in languages.selectable() if lang.code not in chosen]
    ordered = chosen + rest
    items = [
        (f"{g.cached if code in chosen else ' '}  {languages.name(code)}  ({code})", code)
        for code in ordered
    ]
    return picker.fzf_multi(items, f"{label}> ", header="TAB: (de)seleziona · INVIO: conferma")


# Debrid providers Torrentio supports (config-string key, display name), RD first.
# The whole `torrentio_base` is passed to Torrentio as-is, so switching provider is
# just swapping this key — stream resolution already works for any of them.
_PROVIDERS: list[tuple[str, str]] = [
    ("realdebrid", "RealDebrid"),
    ("alldebrid", "AllDebrid"),
    ("premiumize", "Premiumize"),
    ("torbox", "TorBox"),
    ("debridlink", "Debrid-Link"),
    ("easydebrid", "EasyDebrid"),
    ("offcloud", "Offcloud"),
    ("putio", "Put.io"),
]
_PROVIDER_KEYS = {k for k, _ in _PROVIDERS}


def _token_status(cfg: Config) -> str:
    for key, name in _PROVIDERS:
        m = re.search(rf"(?:^|\|){re.escape(key)}=([^|]*)", cfg.torrentio_base)
        if m and m.group(1):
            return f"✓ {name} (••••)"
    return "✗ assente"


def _backend_status(cfg: Config) -> str:
    if cfg.playback_backend == "debrid":
        return "debrid (premium, via Torrentio)"
    if cfg.playback_backend == "native":
        creds = config.debrid_credentials(cfg.torrentio_base)
        provider = creds[0] if creds else "?"
        ok = creds is not None and debrid.supports_native(provider)
        return f"debrid nativo · {provider}" + ("" if ok else " ⚠ provider non supportato")
    health = "TorrServer ✓" if engine.installed() else "TorrServer ✗ (installalo)"
    return f"P2P locale · {health}"


def _with_token(base: str, token: str, provider: str = "realdebrid") -> str:
    """Set `provider`'s token in the Torrentio config string, dropping any other
    debrid provider segment (Torrentio expects one) and keeping sort/other options.
    Seeds `sort=qualitysize` when the base has none."""
    segs = [s for s in base.split("|") if s and s.split("=", 1)[0] not in _PROVIDER_KEYS]
    if not any(s.startswith("sort=") for s in segs):
        segs.insert(0, "sort=qualitysize")
    segs.append(f"{provider}={token}")
    return "|".join(segs)


def _items(cfg: Config) -> list[tuple[str, str, str, str, str]]:
    """(key, label, kind, current_value, help) for each setting row."""
    return [
        (
            "audio_langs",
            "Lingue audio",
            "langs",
            ", ".join(languages.name(c) for c in cfg.audio_langs) or "—",
            "Lingue audio preferite (--alang e ranking). TAB per (de)selezionare, in ordine.",
        ),
        (
            "subtitle_langs",
            "Lingue sottotitoli",
            "langs",
            ", ".join(languages.name(c) for c in cfg.subtitle_langs) or "—",
            "Lingue sottotitoli preferite (--slang e --subs). TAB per (de)selezionare, in ordine.",
        ),
        (
            "auto_play",
            "Riproduzione automatica",
            "bool",
            "on" if cfg.auto_play else "off",
            "Invio sul titolo avvia subito il migliore; Tab apre la scelta manuale. Off = inverte.",
        ),
        (
            "prefer_cast",
            "Riproduzione su Chromecast",
            "bool",
            "on" if cfg.prefer_cast else "off",
            "Manda lo stream al Chromecast (catt). --cast/--local forzano per la sessione.",
        ),
        (
            "cast_device",
            "Dispositivo cast",
            "castdev",
            cfg.cast_device or "auto (scoperta)",
            "Chromecast preferito (scoperta via catt). 'auto' = per-LAN (catt scan).",
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
            "lang_filter",
            "Filtra per lingua",
            "bool",
            "on" if cfg.lang_filter else "off",
            "Sposta in fondo gli stream taggati solo con lingue diverse dalle tue audio_langs.",
        ),
        (
            "exclude_camrip",
            "Escludi camrip",
            "bool",
            "on" if cfg.exclude_camrip else "off",
            "Sposta in fondo le release CAM/TS/TC/SCR (registrate al cinema, pessima qualità).",
        ),
        (
            "min_seeders",
            "Seeder minimi",
            "int",
            str(cfg.min_seeders),
            "Stream non-cached sotto questa soglia (quasi morti) finiscono in fondo. 0 = off.",
        ),
        (
            "dedup",
            "Rimuovi doppioni",
            "bool",
            "on" if cfg.dedup else "off",
            "Collassa la stessa release vista su più tracker, tiene la migliore.",
        ),
        (
            "max_streams",
            "Max stream mostrati",
            "int",
            str(cfg.max_streams) if cfg.max_streams else "tutti",
            "Quanti stream mostrare prima di «mostra tutti». 0 = nessun limite.",
        ),
        (
            "nerd_font",
            "Icone Nerd Font",
            "choice",
            cfg.nerd_font,
            "Glyph Nerd Font nei menu/anteprima. auto = via env NSTREAM_NERD_FONT; on/off forzano.",
        ),
        (
            "posters",
            "Poster nell'anteprima",
            "bool",
            "on" if cfg.posters else "off",
            "Mostra il poster nel pannello di anteprima fzf (richiede chafa). Off = solo testo.",
        ),
        (
            "image_mode",
            "Immagini nell'anteprima",
            "choice",
            cfg.image_mode,
            "auto = sixel/half-blocks se il terminale li supporta; off = nessuna immagine.",
        ),
        (
            "playback_backend",
            "Backend riproduzione",
            "backend",
            _backend_status(cfg),
            "P2P locale (gratis, TorrServer) · debrid via Torrentio · debrid nativo "
            "(API diretta TorBox/Premiumize, indipendente da Torrentio).",
        ),
        (
            "torrentio_base",
            "Token debrid",
            "token",
            _token_status(cfg),
            "Provider debrid Torrentio (RealDebrid/AllDebrid/TorBox/…) + chiave. Mai loggato.",
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
        lo, hi = config.INT_BOUNDS.get(key, (1, 120))
        raw = _ask(f"{label} ({lo}-{hi}): ")
        if raw:
            try:
                config.save({key: max(lo, min(int(raw), hi))})
            except ValueError:
                print("nstream: valore non valido", file=sys.stderr)
    elif kind == "langs":
        sel = _pick_languages(getattr(cfg, key), label)
        if sel:  # empty/ESC → keep current (avoids an accidental wipe of all preferences)
            config.save({key: sel})
    elif kind == "enum":  # hwdec
        i = _fzf_select(HWDEC_CHOICES, prompt=f"{label}> ")
        if i is not None:
            config.save({key: "" if HWDEC_CHOICES[i].startswith("no") else HWDEC_CHOICES[i]})
    elif kind == "choice":  # fixed value list (nerd_font, image_mode)
        choices = CHOICE_VALUES[key]
        i = _fzf_select(choices, prompt=f"{label}> ")
        if i is not None:
            config.save({key: choices[i]})
    elif kind == "maxres":
        labels = [c[0] for c in MAXRES_CHOICES]
        i = _fzf_select(labels, prompt=f"{label}> ")
        if i is not None:
            config.save({key: MAXRES_CHOICES[i][1]})
    elif kind == "castdev":
        choices = ["(auto)"] + [name for name, _ in scan_devices()]
        i = _fzf_select(
            choices, prompt="dispositivo> ", header="Chromecast preferito · ESC: annulla"
        )
        if i is not None:
            config.save({"cast_device": "" if i == 0 else choices[i]})
    elif kind == "backend":
        choices = [
            "P2P locale (gratis)",
            "debrid via Torrentio (premium)",
            "debrid nativo — API diretta (TorBox/Premiumize)",
        ]
        i = _fzf_select(
            choices, prompt="backend> ", header="Sorgente di riproduzione · ESC: annulla"
        )
        if i is None:
            return
        if i == 0:
            config.save({"playback_backend": "local"})
            if not engine.installed():
                print(
                    f"nstream: {engine.BINARY} non installato — "
                    "installalo (es. `yay -S torrserver-bin`) per lo streaming P2P",
                    file=sys.stderr,
                )
        elif i == 1:
            config.save({"playback_backend": "debrid"})
            if _token_status(cfg).startswith("✗"):
                print("nstream: imposta un token debrid qui sotto per usarlo", file=sys.stderr)
        else:
            config.save({"playback_backend": "native"})
            creds = config.debrid_credentials(cfg.torrentio_base)
            if creds is None or not debrid.supports_native(creds[0]):
                supported = ", ".join(debrid.NATIVE_PROVIDERS)
                print(
                    f"nstream: il backend nativo supporta {supported} — "
                    "imposta uno di questi token qui sotto",
                    file=sys.stderr,
                )
    elif kind == "token":
        i = _fzf_select([name for _, name in _PROVIDERS], prompt="provider> ")
        if i is None:
            return
        provider_key, provider_name = _PROVIDERS[i]
        token = getpass.getpass(f"Chiave {provider_name} (nascosta): ").strip()
        if token:
            config.save({"torrentio_base": _with_token(cfg.torrentio_base, token, provider_key)})
            print(f"nstream: token {provider_name} aggiornato", file=sys.stderr)


# --- addons submenu ------------------------------------------------------


def _addons_menu(cfg: Config) -> None:
    while True:
        eff = addons.effective_addons(cfg)
        rows: list[str] = []
        for a in eff:
            suffix = "  · built-in" if a.builtin else ""
            rows.append(f"🧩 {a.name:18s} {','.join(a.resources)}{suffix}")
        rows.append("➕ Aggiungi addon…")
        idx = _fzf_select(
            rows, prompt="plugin> ", header="INVIO: aggiungi / rimuovi (solo extra) · ESC: indietro"
        )
        if idx is None:  # ESC backs out, like every other menu
            return
        if idx == len(rows) - 1:
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
    """First-run: pick the playback backend. Local P2P (free, default) needs no key and writes
    a token-less config; debrid (via Torrentio) and native (direct provider API) ask for a
    provider + API key. The native option restricts the provider list to those with a resolver."""
    print("Primo avvio nstream — scegli come riprodurre.", file=sys.stderr)
    i = _fzf_select(
        [
            "P2P locale — gratis, nessuna chiave (consigliato)",
            "Debrid via Torrentio — premium, serve una chiave",
            "Debrid nativo — API diretta (TorBox/Premiumize)",
        ],
        prompt="backend> ",
        header="P2P locale streama i torrent in locale; il debrid usa stream cached a pagamento",
    )
    if i is None or i == 0:  # default to local (also when ESC: no paid signup required)
        config.save({"playback_backend": "local"})
        if not engine.installed():
            print(
                f"nstream: per lo streaming P2P installa {engine.BINARY} "
                "(es. `yay -S torrserver-bin`).",
                file=sys.stderr,
            )
        return
    backend = "debrid" if i == 1 else "native"
    # Native resolves through the provider's own API, so only providers with a resolver qualify.
    providers = (
        [p for p in _PROVIDERS if debrid.supports_native(p[0])]
        if backend == "native"
        else _PROVIDERS
    )
    j = _fzf_select(
        [name for _, name in providers],
        prompt="provider> ",
        header="Scegli il debrid (poi inserisci la chiave API dal suo sito)",
    )
    if j is None:
        config.save({"playback_backend": "local"})  # backed out → safe free default
        return
    provider_key, provider_name = providers[j]
    try:
        token = getpass.getpass(f"Chiave {provider_name} (nascosta): ").strip()
    except EOFError:
        token = ""
    if not token:
        config.save({"playback_backend": "local"})  # no key → fall back to free local
        return
    config.save(
        {"torrentio_base": _with_token("", token, provider_key), "playback_backend": backend}
    )
