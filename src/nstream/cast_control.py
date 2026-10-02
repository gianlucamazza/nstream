"""Interactive cast lifecycle helpers (stop / status / volume / pause / seek) shared by the TUI.

Reuses the same caster/bridge/remux/mirror primitives as headless `--json --stop` etc.,
without emitting JSON. Sits below `cli` (never imports it).
"""

from __future__ import annotations

import shutil

from . import bridge, caster, mirror, remux, state, ui, util
from .config import Config


def resolve_session_device(prefer: str | None = None) -> str | None:
    """Device IP/name for lifecycle actions: explicit prefer, else live session, else None."""
    if prefer:
        return prefer
    return state.cast_session_device()


def stop_cast(cfg: Config, *, device: str | None = None) -> tuple[bool, str]:
    """Stop the active cast (mirror + DMR + remux server). Returns (ok, user message)."""
    state.expire_cast_session()
    mirror_stopped = mirror.stop()
    dev = resolve_session_device(device)
    if dev is None:
        # No (or expired) session: still reclaim a detached remux/subtitle server left by
        # an earlier cast — remux.stop falls back to the device it recorded.
        local_stopped = remux.stop(None)
        if mirror_stopped or local_stopped:
            return True, "mirror fermato" if mirror_stopped else "server locale fermato"
        return False, "nessun cast attivo"
    st = caster.status(dev)
    ok = caster.stop(dev)
    if bridge.bridge_available():
        ok = bridge.stop(dev) or ok
    ok = remux.stop(dev) or ok
    ok = ok or mirror_stopped
    state.update_from_receiver(
        cfg,
        dev,
        st.get("position") or 0.0,
        st.get("duration") or 0.0,
        title=st.get("title"),
        clear=True,
    )
    return (ok, f"cast fermato su {dev}" if ok else f"stop fallito su {dev}")


def cast_status(*, device: str | None = None) -> tuple[bool, str]:
    """One-line status of the receiver (and refresh session resume when possible)."""
    state.expire_cast_session()
    dev = resolve_session_device(device)
    if dev is None:
        return False, "nessun cast attivo"
    st = caster.status(dev)
    title = st.get("title") or "?"
    state_s = st.get("player_state") or "?"
    pos = float(st.get("position") or 0.0)
    dur = float(st.get("duration") or 0.0)
    vol = st.get("volume")
    bits = [str(state_s), str(title)]
    if dur > 0:
        bits.append(f"{pos:.0f}/{dur:.0f}s")
    elif pos > 0:
        bits.append(f"{pos:.0f}s")
    if isinstance(vol, (int, float)):
        pct = int(vol * 100) if float(vol) <= 1.0 else int(vol)
        bits.append(f"vol {pct}%")
    return True, f"{dev}: " + " · ".join(bits)


def refresh_session_from_status(cfg: Config, *, device: str | None = None) -> None:
    """Merge receiver position into history (best-effort). Used after status poll."""
    dev = resolve_session_device(device)
    if not dev:
        return
    st = caster.status(dev)
    state.update_from_receiver(
        cfg,
        dev,
        st.get("position") or 0.0,
        st.get("duration") or 0.0,
        title=st.get("title"),
    )


def set_cast_volume(level: int, *, device: str | None = None) -> tuple[bool, str]:
    """Set receiver volume 0–100. Returns (ok, message)."""
    level = max(0, min(100, int(level)))
    dev = resolve_session_device(device)
    if dev is None:
        return False, "nessun cast attivo"
    ok = caster.set_volume(dev, level)
    return (ok, f"volume {level}% su {dev}" if ok else f"volume non impostato su {dev}")


def media_control(
    cmd: str,
    *,
    value: float = 0.0,
    device: str | None = None,
) -> tuple[bool, str]:
    """pause / resume (play) / seek on the active receiver. Prefers castbridge, falls back to catt.

    `cmd` is one of: pause, play, seek. `value` is the seek target in seconds when cmd=seek.
    """
    dev = resolve_session_device(device)
    if dev is None:
        return False, "nessun cast attivo"
    cmd = cmd.strip().lower()
    if cmd not in ("pause", "play", "seek"):
        return False, f"comando media sconosciuto: {cmd}"
    # A long jump on a live cast (ADR 0039) is a re-LOAD at the target; None = not live.
    reloaded = remux.live_seek(dev, value) if cmd == "seek" else None
    if reloaded is not None:
        ok = reloaded
    else:
        ok = bridge.bridge_available() and bridge.control(dev, cmd, value)
    if not ok and reloaded is None:
        if cmd == "seek":
            catt_args = ["seek", str(int(value))]
        elif cmd == "pause":
            catt_args = ["pause"]
        else:
            catt_args = ["play"]
        res = util.run_cmd(["catt", "-d", dev, *catt_args], timeout=util.CATT_INFO_TIMEOUT)
        ok = bool(res and res.returncode == 0)
    if cmd == "seek":
        return ok, (f"seek {int(value)}s su {dev}" if ok else f"seek fallito su {dev}")
    if cmd == "pause":
        return ok, (f"pausa su {dev}" if ok else f"pausa fallita su {dev}")
    return ok, (f"ripresa su {dev}" if ok else f"ripresa fallita su {dev}")


def shift_subtitles(delta: float, device: str | None = None) -> tuple[bool, str]:
    """Move the live cast's subtitles by `delta` seconds (TUI cast menu)."""
    dev = resolve_session_device(device)
    if dev is None:
        return False, "nessun cast attivo"
    total = remux.live_sub_shift(dev, delta)
    if total is None:
        return False, "sottotitoli spostabili solo su un cast in diretta"
    return True, f"sottotitoli {total:+.1f}s"


def runtime_health() -> list[tuple[str, bool, str]]:
    """(name, ok, detail) for key optional/required runtime deps — settings diagnostics."""
    from . import engine

    rows: list[tuple[str, bool, str]] = []
    mpv_ok = shutil.which("mpv") is not None
    rows.append(("mpv", mpv_ok, "ok" if mpv_ok else "mancante (richiesto)"))
    fzf_ok = shutil.which("fzf") is not None
    rows.append(("fzf", fzf_ok, "ok" if fzf_ok else "mancante (richiesto)"))
    ts_ok = engine.installed()
    rows.append(
        (
            "TorrServer",
            ts_ok,
            "ok" if ts_ok else "mancante (P2P locale)",
        )
    )
    catt_ok = shutil.which("catt") is not None
    rows.append(("catt", catt_ok, "ok" if catt_ok else "mancante (discovery/cast fallback)"))
    br_ok = bridge.bridge_available()
    rows.append(
        (
            "castbridge",
            br_ok,
            "ok" if br_ok else "assente → fallback catt",
        )
    )
    mir_ok = mirror.available()
    rows.append(
        (
            "mirror sender",
            mir_ok,
            "ok" if mir_ok else "assente ($CAST_MIRROR_BIN / stack Hyprland)",
        )
    )
    ff_ok = shutil.which("ffprobe") is not None
    rows.append(("ffprobe", ff_ok, "ok" if ff_ok else "mancante (tracce/remux)"))
    return rows


def health_summary() -> str:
    """Compact one-line status for a settings row."""
    parts = []
    for name, ok, _ in runtime_health():
        mark = ui.g().cached if ok else ui.g().fail
        parts.append(f"{mark}{name}")
    return " ".join(parts)
