"""Standalone catt.api LOAD helper (no nstream imports).

Run with the interpreter that has catt installed (the `catt` console-script
shebang). Stdin is one JSON object: url plus ip or name, optional title/thumb/
content_type/stream_type/current_time/subtitle_url/media_info. Never prints the
media URL.
Exit 0 on play_media_url success. Used when nstream's env cannot `import catt`.
"""

from __future__ import annotations

import importlib
import json
import sys


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
    try:
        # play_media_url (not play_url): one LOAD, no yt-dlp, no 10s PLAYING wait.
        # nstream polls the receiver. Never print args["url"].
        ctor = {"ip_addr": str(ip)} if ip else {"name": str(name)}
        device = api.CattDevice(**ctor)
        device.controller.prep_app()
        device.controller.play_media_url(str(args["url"]), **kwargs)
    except Exception:  # noqa: BLE001 — helper: any catt/pychromecast failure is rc 1
        print("catt-load: play_media_url failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
