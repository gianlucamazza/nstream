"""Standalone catt.api LOAD helper (no nstream imports).

Run with the interpreter that has catt installed (the `catt` console-script
shebang). Stdin is one JSON object: url plus ip or name, optional title/thumb/
content_type/stream_type/current_time/subtitle_url/media_info. Never prints the
media URL.

Exit codes:
  0  play_media_url succeeded (media session active)
  1  LOAD never sent (connect / prep / play_media raise)
  2  bad stdin / missing url or device
  3  catt.api not importable
  4  LOAD sent; catt's media-session wait timed out (post-LOAD CastError)

Used when nstream's env cannot `import catt`.
"""

from __future__ import annotations

import importlib
import json
import sys

# catt 0.13.3 DefaultCastController.play_media_url after play_media():
#   raise CastError("Waiting for the media session to become active timed out after 30 seconds")
_CATT_SESSION_WAIT = "media session to become active timed out"


def _is_catt_session_wait(exc: BaseException) -> bool:
    return type(exc).__name__ == "CastError" and _CATT_SESSION_WAIT in str(exc)


def _hook_play_media(controller, sent: dict):
    """Wrap play_media. Returns restore() or None. Honour sent['abandoned']."""
    inner = getattr(controller, "_controller", None)
    if inner is None:
        return None
    play = getattr(inner, "play_media", None)
    if not callable(play):
        return None

    def wrapped(*a, **k):
        if sent.get("abandoned"):
            return None
        out = play(*a, **k)
        sent["v"] = True
        return out

    inner.play_media = wrapped

    def restore() -> None:
        inner.play_media = play

    return restore


def main() -> int:
    try:
        args = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        print("catt-load: invalid json", file=sys.stderr)
        return 2
    if not isinstance(args, dict) or not args.get("url"):
        print("catt-load: url required", file=sys.stderr)
        return 2
    ip = args.get("ip") or args.get("ip_addr")
    name = args.get("name")
    if not ip and not name:
        print("catt-load: ip or name required", file=sys.stderr)
        return 2
    try:
        api = importlib.import_module("catt.api")
    except ImportError:
        print("catt-load: catt.api not importable", file=sys.stderr)
        return 3
    kwargs: dict = {}
    for key in ("title", "thumb", "content_type", "stream_type"):
        val = args.get(key)
        if val:
            kwargs[key] = val
    if args.get("current_time"):
        kwargs["current_time"] = args["current_time"]
    if args.get("media_info"):
        kwargs["media_info"] = args["media_info"]
    if args.get("subtitle_url"):
        kwargs["subtitles"] = args["subtitle_url"]
    sent = {"v": False, "abandoned": False}
    restore = None
    try:
        # play_media_url (not play_url): one LOAD, no yt-dlp, no 10s PLAYING wait.
        # nstream polls the receiver. Never print args["url"].
        ctor = {"ip_addr": str(ip)} if ip else {"name": str(name)}
        device = api.CattDevice(**ctor)
        restore = _hook_play_media(device.controller, sent)
        device.controller.prep_app()
        device.controller.play_media_url(str(args["url"]), **kwargs)
    except Exception as exc:  # noqa: BLE001 — helper: classify sent vs never-sent
        if _is_catt_session_wait(exc) or sent["v"]:
            print("catt-load: media session unconfirmed", file=sys.stderr)
            return 4
        print("catt-load: play_media_url failed", file=sys.stderr)
        return 1
    finally:
        if restore is not None:
            restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
