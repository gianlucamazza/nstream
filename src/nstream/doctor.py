"""Read-only local installation diagnostics; no network or backend launches."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys

from . import __version__, mirror
from .config import ConfigError, config_path, load


def inspect() -> dict:
    checks: list[dict] = []
    for name in ("mpv", "fzf", "ffmpeg", "ffprobe", "catt", "chafa", "vainfo", "castbridge"):
        checks.append(
            {
                "name": name,
                "required": name in ("mpv", "fzf"),
                "status": "ok" if shutil.which(name) else "missing",
            }
        )
    # Not required, but its absence silently disables the mirror fallbacks for huge or
    # .mkv remuxes (ADR 0015/0022), so name the reason.
    why = mirror.unavailable_reason()
    mirror_check = {"name": "mirror", "required": False, "status": "missing" if why else "ok"}
    if why:
        mirror_check["detail"] = why
    checks.append(mirror_check)
    try:
        load(secure_permissions=False)
        private = stat.S_IMODE(config_path().stat().st_mode) & 0o077 == 0
        status = "ok" if private else "permissions"
    except (ConfigError, OSError, TypeError, ValueError):
        status = "invalid_or_missing"
    checks.append({"name": "config", "required": True, "status": status})
    return {
        "ok": all(c["status"] == "ok" for c in checks if c["required"]),
        "action": "doctor",
        "version": __version__,
        "python": sys.version.split()[0],
        "checks": checks,
        "session": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        "p2p_privacy": "interface detection is heuristic; routing and leak protection unverified",
        "network_tested": False,
        "playback_tested": False,
    }


def run(*, json_mode: bool = False) -> int:
    report = inspect()
    if json_mode:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"nstream {report['version']} — diagnostica locale")
        for check in report["checks"]:
            detail = f" ({check['detail']})" if check.get("detail") else ""
            print(f"{check['name']}: {check['status']}{detail}")
        print("Rete, protezione VPN e riproduzione non verificate.")
    return 0 if report["ok"] else 1
