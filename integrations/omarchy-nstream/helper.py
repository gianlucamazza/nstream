"""Data and actions for the gianlucamazza.nstream Omarchy bar widget.

Every invocation prints exactly one JSON object on stdout. The widget never talks to
nstream directly: this helper validates every argument, runs `nstream --json …` as argv
(never through a shell) and forwards only bounded, descriptive fields. nstream itself
never emits stream URLs or tokens in `--json` output (docs/headless.md).

Usage: helper.py open | history | status | pause | resume | stop | seek SEC | seekto SEC |
       volume PCT | subshift SEC | continue TARGET TITLE
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unicodedata

MAX_TITLE = 200
MAX_ENTRIES = 30
MAX_OUTPUT = 1 << 20
TARGETS = ("cast", "local")
# A cast LOAD can take a while (debrid resolve, remux probe); control calls are quick.
CAST_TIMEOUT = 240
CONTROL_TIMEOUT = 30


def emit(obj: dict) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return 0


def fail(kind: str, message: str) -> int:
    return emit({"kind": kind, "ok": False, "error": message[:300]})


def nstream_bin() -> str | None:
    # The shell's environment may not carry ~/.local/bin (where `uv tool` installs).
    return shutil.which("nstream", path=child_env()["PATH"])


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([env.get("PATH", ""), os.path.expanduser("~/.local/bin")])
    return env


def text(value: object, limit: int = MAX_TITLE) -> str:
    """Bounded single-line text with control characters removed."""
    s = "".join(c for c in str(value or "") if unicodedata.category(c)[0] != "C")
    return s.strip()[:limit]


def number(value: object) -> float:
    if not isinstance(value, (int, float, str)):
        return 0.0
    try:
        n = float(value)
    except (ValueError, OverflowError):
        return 0.0
    return n if n == n and abs(n) < 1e7 else 0.0  # drop NaN / absurd values


def run(args: list[str], timeout: float | None) -> dict:
    """Run `nstream --json ARGS` and return its JSON object (or an error dict)."""
    exe = nstream_bin()
    if exe is None:
        return {"ok": False, "error": "missing", "message": "nstream non è installato"}
    try:
        # Without --follow, stdout contains only the final JSON result. Diagnostic
        # stderr is already covered by nstream's log, including during long local plays.
        p = subprocess.run(
            [exe, "--json", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=child_env(),
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "message": "nstream non ha risposto in tempo"}
    except OSError:
        return {"ok": False, "error": "process", "message": "impossibile avviare nstream"}
    raw = p.stdout
    if len(raw) > MAX_OUTPUT:
        return {"ok": False, "error": "internal", "message": "risposta di nstream troppo grande"}
    lines = [ln for ln in raw.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    for line in reversed(lines):  # --follow aside, the result is the last JSON line
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            if p.returncode and data.get("ok"):
                return {"ok": False, "error": "process", "message": "nstream terminato con errore"}
            return data
    return {"ok": False, "error": "internal", "message": "risposta di nstream non valida"}


def message_of(data: dict) -> str:
    """The human message of a failed result, or its first notice."""
    if data.get("message"):
        return text(data["message"], 300)
    notes = data.get("notices")
    if isinstance(notes, list) and notes and isinstance(notes[0], dict):
        return text(notes[0].get("text"), 300)
    return text(data.get("error") or "errore", 300)


def prefer_cast() -> bool:
    """`prefer_cast` from nstream's config — read for the default target only."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    try:
        with open(os.path.join(base, "nstream", "config.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("prefer_cast", False))
    except (OSError, ValueError, AttributeError):
        return False


def cmd_history() -> int:
    data = run(["--history"], CONTROL_TIMEOUT)
    if data.get("error") == "usage":  # an nstream from before --history
        return fail("history", "nstream è troppo vecchio: aggiornalo (serve --json --history)")
    if not data.get("ok"):
        return fail("history", message_of(data))
    entries = []
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        return fail("history", "cronologia di nstream non valida")
    for e in raw_entries[:MAX_ENTRIES]:
        if not isinstance(e, dict) or not text(e.get("title")):
            continue
        season, episode = e.get("season"), e.get("episode")
        entries.append(
            {
                "title": text(e.get("title")),
                "type": "series" if e.get("type") == "series" else "movie",
                "season": max(0, min(10000, season)) if isinstance(season, int) else 0,
                "episode": max(0, min(10000, episode)) if isinstance(episode, int) else 0,
                "position": max(0.0, number(e.get("position"))),
                "duration": max(0.0, number(e.get("duration"))),
                "next": bool(e.get("next")),
            }
        )
    session = data.get("cast_session")
    if isinstance(session, dict):
        session = {"title": text(session.get("title")), "device": text(session.get("device"), 80)}
    else:
        session = None
    return emit(
        {
            "kind": "history",
            "ok": True,
            "entries": entries,
            "session": session,
            "preferCast": prefer_cast(),
        }
    )


def cmd_status() -> int:
    data = run(["--status"], CONTROL_TIMEOUT)
    if not data.get("ok"):
        return fail("status", message_of(data))
    if data.get("receiver_error"):
        # Only forward an error label: receiver payloads can contain media URLs.
        return fail("status", "il ricevitore segnala un errore di riproduzione")
    return emit(
        {
            "kind": "status",
            "ok": True,
            "device": text(data.get("device"), 80),
            "title": text(data.get("title")),
            "state": text(data.get("player_state"), 20).upper(),
            "position": max(0.0, number(data.get("position"))),
            "duration": max(0.0, number(data.get("duration"))),
            # -1 = the receiver did not report a level (the panel hides the slider).
            "volume": (
                round(max(0.0, min(100.0, number(data["volume_percent"]))))
                if data.get("volume_percent") is not None
                else -1
            ),
        }
    )


def cmd_action(action: str, args: list[str], timeout: float | None = CONTROL_TIMEOUT) -> int:
    data = run(args, timeout)
    if not data.get("ok"):
        return fail(action, message_of(data))
    out = {"kind": action, "ok": True}
    if action in ("continue", "local"):
        out["title"] = text(data.get("title"))
        out["device"] = text(data.get("device"), 80)
        out["notice"] = text(data.get("notice"), 300) or (
            message_of(data) if data.get("notices") else ""
        )
    return emit(out)


def bounded(raw: str, lo: float, hi: float) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if lo <= value <= hi else None


def cmd_continue(target: str, title: str) -> int:
    title = text(title)
    if target not in TARGETS or not title:
        return fail("continue", "argomenti non validi")
    if target == "cast":
        # Fire-and-return: nstream returns once the receiver has the media.
        return cmd_action("continue", ["-c", "--cast", "--", title], CAST_TIMEOUT)
    # A separate QML Process waits for the final result without blocking cast controls.
    # The child starts a new session, so closing/reloading the widget leaves mpv alive.
    return cmd_action("local", ["-c", "--local", "--", title], None)


def cmd_open() -> int:
    env = child_env()
    exe = shutil.which("nstream-fuzzel", path=env["PATH"])
    if exe is None or shutil.which("foot", path=env["PATH"]) is None:
        return fail("open", "servono nstream-fuzzel e foot per aprire nstream")
    try:
        subprocess.Popen(
            [exe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except OSError:
        return fail("open", "impossibile aprire nstream")
    return emit({"kind": "open", "ok": True})


def main(argv: list[str]) -> int:
    if not argv:
        return fail("usage", "comando mancante")
    cmd, rest = argv[0], argv[1:]
    if cmd == "open" and not rest:
        return cmd_open()
    if cmd == "history" and not rest:
        return cmd_history()
    if cmd == "status" and not rest:
        return cmd_status()
    if cmd in ("pause", "resume", "stop") and not rest:
        return cmd_action(cmd, ["--" + cmd])
    if cmd == "seekto" and len(rest) == 1:
        sec = bounded(rest[0], 0, 1e6)
        if sec is None:
            return fail(cmd, "posizione non valida")
        return cmd_action(cmd, ["--seek", str(int(sec))])
    if cmd == "seek" and len(rest) == 1:
        delta = bounded(rest[0], -3600, 3600)
        if delta is None:
            return fail(cmd, "salto non valido")
        status = run(["--status"], CONTROL_TIMEOUT)
        if not status.get("ok"):
            return fail(cmd, message_of(status))
        target = max(0.0, number(status.get("position")) + delta)
        duration = number(status.get("duration"))
        if duration > 0:
            target = min(target, max(0.0, duration - 5))
        return cmd_action(cmd, ["--seek", str(int(target))])
    if cmd == "volume" and len(rest) == 1:
        pct = bounded(rest[0], 0, 100)
        if pct is None:
            return fail(cmd, "volume non valido")
        return cmd_action(cmd, ["--volume", str(int(pct))])
    if cmd == "subshift" and len(rest) == 1:
        sec = bounded(rest[0], -30, 30)
        if sec is None:
            return fail(cmd, "spostamento non valido")
        return cmd_action(cmd, ["--sub-shift", str(sec)])
    if cmd == "continue" and len(rest) == 2:
        return cmd_continue(rest[0], rest[1])
    return fail("usage", "comando non valido")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
