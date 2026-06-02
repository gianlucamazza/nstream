"""Command-line entry point: search/browse → pick (fzf) → play (mpv)."""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable

from . import __version__, api, state
from .config import Config, ConfigError, HistoryEntry, Meta, Stream, Subtitle, Video, load

# --browse keyword → Cinemeta catalog id.
CAT_MAP = {"popolari": "top", "nuovi": "year", "top": "imdbRating"}


def fzf[T](items: list[tuple[str, T]], prompt: str) -> T | None:
    """Pick one of (label, value) pairs via fzf. Returns the value or None."""
    if not items:
        return None
    if len(items) == 1:
        return items[0][1]
    # Hidden leading index lets labels repeat without ambiguity.
    lines = "".join(f"{i}\t{label}\n" for i, (label, _) in enumerate(items))
    try:
        proc = subprocess.run(
            ["fzf", "--prompt", prompt, "--with-nth", "2..",
             "--delimiter", "\t", "--no-sort", "--reverse", "--height", "80%"],
            input=lines, capture_output=True, text=True,
        )  # fmt: skip
    except FileNotFoundError:
        print("nstream: fzf non trovato", file=sys.stderr)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return items[int(proc.stdout.split("\t", 1)[0])][1]


def meta_label(m: Meta) -> str:
    return f"{m.get('type', '?'):6s} {m.get('name', '?')}  ({m.get('releaseInfo', '')})"


def stream_label(s: Stream) -> str:
    name = (s.get("name") or "").replace("\n", " ")
    title = (s.get("title") or "").replace("\n", " · ")
    return f"{name}  |  {title}"[:200]


def history_label(e: HistoryEntry) -> str:
    title = e.get("title", "?")
    if e.get("type") == "series" and e.get("season"):
        title += f"  S{e.get('season', 0):02d}E{e.get('episode', 0):02d}"
    dur = e.get("duration") or 0.0
    pct = f"  · {e.get('position', 0.0) / dur * 100:.0f}%" if dur else ""
    return f"{title}{pct}"


# --- subtitles ------------------------------------------------------------


def _download_subtitle(sub: Subtitle) -> str | None:
    url = sub.get("url")
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": api.UA})
        with urllib.request.urlopen(req, timeout=api.TIMEOUT) as resp:
            raw = resp.read()
    except OSError:
        print("nstream: download sottotitolo fallito", file=sys.stderr)
        return None
    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        with contextlib.suppress(OSError):
            raw = gzip.decompress(raw)
    fd, path = tempfile.mkstemp(prefix=f"nstream-{sub.get('lang', 'sub')}-", suffix=".srt")
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path


def pick_subtitles(cfg: Config, typ: str, video_id: str) -> tuple[str, ...]:
    try:
        subs = api.subtitles(cfg, typ, video_id)
    except api.NetworkError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return ()
    if not subs:
        print("nstream: nessun sottotitolo", file=sys.stderr)
        return ()
    pref = {lang: i for i, lang in enumerate(cfg.subtitle_langs)}
    subs.sort(key=lambda s: pref.get(s.get("lang", ""), len(pref)))
    items = [(f"{s.get('lang', '?'):5s} {s.get('id', '')}", s) for s in subs]
    chosen = fzf(items, "sottotitolo> ")
    if not chosen:
        return ()
    path = _download_subtitle(chosen)
    return (path,) if path else ()


# --- playback + position tracking ----------------------------------------


def _track_position(sock_path: str, holder: dict[str, float], proc: subprocess.Popen) -> None:
    """Observe mpv's time-pos/duration over the IPC socket; record the last values."""
    sock: socket.socket | None = None
    for _ in range(50):
        if proc.poll() is not None:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(sock_path)
            break
        except OSError:
            sock = None
            time.sleep(0.1)
    if sock is None:
        return
    try:
        for cid, prop in ((1, "time-pos"), (2, "duration")):
            sock.sendall(f'{{"command":["observe_property",{cid},"{prop}"]}}\n'.encode())
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("event") == "property-change" and msg.get("data") is not None:
                    name = msg.get("name")
                    if name in ("time-pos", "duration"):
                        holder["position" if name == "time-pos" else "duration"] = float(
                            msg["data"]
                        )
    except OSError:
        pass
    finally:
        sock.close()


def play(
    cfg: Config,
    title: str,
    url: str,
    *,
    start: float | None = None,
    sub_paths: tuple[str, ...] = (),
) -> tuple[float, float]:
    holder = {"position": 0.0, "duration": 0.0}
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    sock_path = os.path.join(runtime, f"nstream-mpv-{os.getpid()}.sock")
    args = ["mpv", f"--force-media-title={title}", *cfg.mpv_args]
    if start and start > 1:
        args.append(f"--start={start:.0f}")
    args += [f"--sub-file={p}" for p in sub_paths]
    args += [f"--input-ipc-server={sock_path}", url]
    try:
        proc = subprocess.Popen(args)
    except FileNotFoundError:
        print("nstream: mpv non trovato", file=sys.stderr)
        return (0.0, 0.0)
    tracker = threading.Thread(target=_track_position, args=(sock_path, holder, proc), daemon=True)
    tracker.start()
    proc.wait()
    tracker.join(timeout=1.0)
    with contextlib.suppress(OSError):
        os.unlink(sock_path)
    return (holder["position"], holder["duration"])


# --- flow ----------------------------------------------------------------


def resolve_video_id(cfg: Config, meta: Meta) -> tuple[str, Video | None] | None:
    if meta.get("type") != "series":
        return (meta["id"], None)
    vids = api.episodes(cfg, meta["id"])
    items = [
        (f"S{v.get('season', 0):02d}E{v.get('episode', 0):02d}  {v.get('name', '')}", v)
        for v in vids
    ]
    video = fzf(items, "episodio> ")
    if not video:
        return None
    return (video["id"], video)


def _play_video(
    cfg: Config,
    typ: str,
    video_id: str,
    title: str,
    *,
    auto: bool,
    subs: bool,
    history: bool,
    on_save: Callable[[float, float], None] | None,
) -> int:
    results = api.streams(cfg, typ, video_id)
    if not results:
        print("nstream: nessuno stream disponibile", file=sys.stderr)
        return 1
    chosen = results[0] if auto else fzf([(stream_label(s), s) for s in results], "stream> ")
    if not chosen:
        return 0

    sub_paths = pick_subtitles(cfg, typ, video_id) if subs else ()
    start = state.load_history(cfg).get(video_id, {}).get("position") if history else None
    print(f"▶ {title} — {(chosen.get('name') or '').splitlines()[0]}", file=sys.stderr)
    pos, dur = play(cfg, title, chosen["url"], start=start, sub_paths=sub_paths)
    if history and on_save and pos > 0:
        on_save(pos, dur)
    return 0


def play_meta(cfg: Config, meta: Meta, *, auto: bool, subs: bool, history: bool) -> int:
    resolved = resolve_video_id(cfg, meta)
    if not resolved:
        return 0
    video_id, video = resolved
    typ = meta.get("type", "movie")

    def on_save(pos: float, dur: float) -> None:
        entry: HistoryEntry = {
            "video_id": video_id,
            "title": meta.get("name", "?"),
            "type": typ,
            "position": pos,
            "duration": dur,
            "ts": time.time(),
        }
        if video is not None:
            entry["series_id"] = meta.get("id", "")
            entry["season"] = video.get("season", 0)
            entry["episode"] = video.get("episode", 0)
        state.save_entry(cfg, entry)

    return _play_video(
        cfg, typ, video_id, meta.get("name", "nstream"),
        auto=auto, subs=subs, history=history, on_save=on_save,
    )  # fmt: skip


def play_history(cfg: Config, entry: HistoryEntry, *, auto: bool, subs: bool) -> int:
    def on_save(pos: float, dur: float) -> None:
        updated: HistoryEntry = {
            "video_id": entry["video_id"],
            "title": entry.get("title", "?"),
            "type": entry.get("type", "movie"),
            "position": pos,
            "duration": dur,
            "ts": time.time(),
        }
        if entry.get("type") == "series":
            updated["series_id"] = entry.get("series_id", "")
            updated["season"] = entry.get("season", 0)
            updated["episode"] = entry.get("episode", 0)
        state.save_entry(cfg, updated)

    return _play_video(
        cfg, entry.get("type", "movie"), entry["video_id"], entry.get("title", "nstream"),
        auto=auto, subs=subs, history=True, on_save=on_save,
    )  # fmt: skip


def _pick_meta(
    items: list[tuple[str, Meta]], cfg: Config, *, auto: bool, subs: bool, history: bool
) -> int:
    meta = fzf(items, "titolo> ")
    if not meta:
        return 0
    return play_meta(cfg, meta, auto=auto, subs=subs, history=history)


def run_search(cfg: Config, query: str, *, auto: bool, subs: bool, history: bool) -> int:
    metas = api.search(cfg, query)
    if not metas:
        print("nstream: nessun risultato", file=sys.stderr)
        return 1
    return _pick_meta(
        [(meta_label(m), m) for m in metas], cfg, auto=auto, subs=subs, history=history
    )


def run_browse(cfg: Config, cat: str, *, auto: bool, subs: bool, history: bool) -> int:
    metas = api.catalog(cfg, "movie", cat) + api.catalog(cfg, "series", cat)
    if not metas:
        print("nstream: catalogo vuoto", file=sys.stderr)
        return 1
    return _pick_meta(
        [(meta_label(m), m) for m in metas], cfg, auto=auto, subs=subs, history=history
    )


def run_continue(cfg: Config, *, auto: bool, subs: bool, allow_search: bool = False) -> int | None:
    """Play from history. Returns None only if the user picks 'cerca…'."""
    entries = state.recent(cfg)
    if not entries:
        if not allow_search:
            print("nstream: cronologia vuota", file=sys.stderr)
        return None if allow_search else 0
    items: list[tuple[str, HistoryEntry | None]] = [(history_label(e), e) for e in entries]
    if allow_search:
        items.append(("↳ cerca…", None))
    chosen = fzf(items, "continua> ")
    if allow_search and chosen is None:
        # Distinguish 'picked search' (sentinel None value) from 'aborted fzf'.
        # fzf returns the sentinel's value (None) for the search row; an abort
        # also yields None, so treat both as 'fall through to search'.
        return None
    if chosen is None:
        return 0
    return play_history(cfg, chosen, auto=auto, subs=subs)


def _dispatch(cfg: Config, args: argparse.Namespace, history: bool) -> int:
    if args.cont:
        return run_continue(cfg, auto=args.play, subs=args.subs) or 0
    if args.browse:
        return run_browse(
            cfg, CAT_MAP[args.browse], auto=args.play, subs=args.subs, history=history
        )

    query = " ".join(args.query)
    if not query:
        if history and state.recent(cfg):
            result = run_continue(cfg, auto=args.play, subs=args.subs, allow_search=True)
            if result is not None:
                return result
        query = input("cerca> ").strip()
        if not query:
            print("nstream: nessuna query", file=sys.stderr)
            return 2
    return run_search(cfg, query, auto=args.play, subs=args.subs, history=history)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="nstream",
        description="Native Stremio-like client (Cinemeta + Torrentio + Real-Debrid + mpv).",
    )
    parser.add_argument("query", nargs="*", help="titolo da cercare (altrimenti chiede)")
    parser.add_argument("--play", action="store_true", help="riproduci subito il primo stream")
    parser.add_argument("--subs", action="store_true", help="scegli i sottotitoli (OpenSubtitles)")
    parser.add_argument(
        "--browse", nargs="?", const="popolari", choices=list(CAT_MAP),
        help="sfoglia un catalogo Cinemeta invece di cercare (default: popolari)",
    )  # fmt: skip
    parser.add_argument(
        "-c", "--continue", dest="cont", action="store_true",
        help="riprendi dalla cronologia (continua a guardare)",
    )  # fmt: skip
    parser.add_argument("--no-history", action="store_true", help="non salvare la cronologia")
    parser.add_argument("--version", action="version", version=f"nstream {__version__}")
    args = parser.parse_args()

    try:
        cfg = load()
    except ConfigError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return 2

    history = cfg.history_enabled and not args.no_history
    try:
        return _dispatch(cfg, args, history)
    except api.NetworkError as e:
        print(f"nstream: {e}", file=sys.stderr)
        return 1


def _entry() -> None:
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    _entry()
