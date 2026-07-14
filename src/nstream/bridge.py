"""IPC client for the **castbridge** daemon — the native Cast sender that replaces catt on
the metadata path. Drives a metadata-rich LOAD on the Default Media Receiver and consumes the
daemon's `media-status`/`session` events to track playback (started/playing/paused/ended).

Speaks the daemon's AF_UNIX newline-delimited JSON protocol with **stdlib `socket`+`json`
only** (no new runtime dependency), so castbridge is just another external backend beside
catt/mpv/ffprobe. Leaf module (imports `log` + stdlib), never `cli` — like `caster`/`engine`.

The daemon (`castbridge --daemon`) is shared with the LibreWolf cast extension: one instance
per `$XDG_RUNTIME_DIR` (socket `$XDG_RUNTIME_DIR/castbridge/sock`). If nothing is listening we
spawn it under the same `spawn.lock` flock the relay uses, so concurrent starts don't race.
The binary is located like `mirror.py` locates `cast_sender` — an env override, else the
openscreen-fork build output.

Key difference from catt: **the session lives in the daemon, not in our connection.** Once the
LOAD is acknowledged the cast keeps playing even if we disconnect — so a headless
fire-and-return just loads and closes, while `follow` keeps reading events until the session
ends. See `docs/adr/0007`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import stat
import subprocess
import time
from collections.abc import Generator, Iterator

from . import log

_log = log.get_logger("bridge")

# How long to wait for the daemon socket to appear after spawning it.
_DAEMON_START_TIMEOUT = 8.0
_DAEMON_POLL = 0.15
# How long to wait for the LOAD reply / first playing state before giving up.
_LOAD_TIMEOUT = 45.0
# Socket read timeout while following a session (re-armed each loop; a live cast
# pushes a media-status at least on every state/position change).
_FOLLOW_TIMEOUT = 5.0


def _runtime_dir() -> str:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "castbridge")
    return f"/tmp/castbridge-{os.getuid()}"


def _ensure_runtime_dir() -> bool:
    """Create the runtime dir (mode 0o700). On the predictable /tmp fallback (no
    XDG_RUNTIME_DIR) an attacker on a multi-user host could pre-create it, so verify with
    `os.lstat` that it is a real directory (not a symlink), owned by us, mode 0o700 —
    otherwise False and the caller degrades to catt (best-effort, never crash the cast)."""
    d = _runtime_dir()
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        if not os.environ.get("XDG_RUNTIME_DIR"):
            st = os.lstat(d)
            if (
                not stat.S_ISDIR(st.st_mode)
                or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o700
            ):
                _log.warning("runtime dir %s non fidata (symlink/owner/permessi)", d)
                return False
        return True
    except OSError as e:
        _log.warning("runtime dir %s: %s", d, e)
        return False


def _socket_path() -> str:
    return os.path.join(_runtime_dir(), "sock")


def _lock_path() -> str:
    return os.path.join(_runtime_dir(), "spawn.lock")


def _binary() -> str:
    """The castbridge binary (built separately inside the openscreen fork, like cast_sender)."""
    env = os.environ.get("CASTBRIDGE_BIN")
    if env:
        return env
    return os.path.expanduser(
        "~/Workspace/tooling/openscreen-build/openscreen/out/Default/castbridge"
    )


def bridge_available() -> bool:
    """True when the castbridge binary is executable. The caller falls back to catt otherwise
    (graceful degradation: no metadata, current behaviour). A running daemon isn't required —
    `ensure_daemon` spawns it on demand."""
    b = _binary()
    return os.path.isfile(b) and os.access(b, os.X_OK)


def _connect(path: str | None = None) -> socket.socket | None:
    """Connect to the daemon socket; None if nothing is listening (best-effort)."""
    path = path or _socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        return s
    except OSError:
        s.close()
        return None


def ensure_daemon(timeout: float = _DAEMON_START_TIMEOUT) -> bool:
    """Ensure `castbridge --daemon` is running and its socket is reachable. Returns True if a
    connection can be made. Spawns the daemon under the relay's `spawn.lock` flock so
    concurrent starts (a relay + nstream) don't both launch it. Best-effort: any failure → False
    and the caller degrades to catt."""
    s = _connect()
    if s is not None:
        s.close()
        return True
    if not bridge_available():
        return False
    if not _ensure_runtime_dir():
        return False
    lock_fd = None
    try:
        lock_fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Re-check under the lock: another process may have started it meanwhile.
        s = _connect()
        if s is not None:
            s.close()
            return True
        # The daemon must outlive its client. start_new_session detaches the process
        # SESSION but not the cgroup: spawned as a plain child it inherits the first
        # caller's lifecycle domain (terminal scope, systemd service, ...) and gets
        # killed at that supervisor's teardown. On systemd hosts, spawn it into its
        # own transient user unit instead (--collect reaps the unit on exit; a name
        # collision with a live unit is fine — the socket poll below decides). The
        # detached Popen remains as the non-systemd fallback.
        spawned = False
        try:
            rc = subprocess.run(
                [
                    "systemd-run",
                    "--user",
                    "--collect",
                    "--quiet",
                    "--unit=castbridge",
                    "--",
                    _binary(),
                    "--daemon",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            spawned = rc == 0
        except (OSError, subprocess.SubprocessError):
            spawned = False
        if not spawned:
            try:
                subprocess.Popen(
                    [_binary(), "--daemon"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except (OSError, subprocess.SubprocessError) as e:
                _log.warning("impossibile avviare castbridge --daemon: %s", e)
                return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = _connect()
            if s is not None:
                s.close()
                return True
            time.sleep(_DAEMON_POLL)
        _log.warning("castbridge --daemon non in ascolto entro %ss", timeout)
        return False
    except OSError as e:
        _log.warning("ensure_daemon: %s", e)
        return False
    finally:
        if lock_fd is not None:
            with contextlib.suppress(OSError):
                os.close(lock_fd)


def _send(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode())


def _messages(sock: socket.socket) -> Iterator[dict | None]:
    """Yield decoded newline-delimited JSON objects from the socket until EOF (a falsy `recv`).
    A socket read **timeout** yields a single `None` *tick* and keeps going — the generator must
    survive timeouts (a raised exception would finalize it, ending the follow loop on the first
    quiet stretch). Any other socket error ends the stream. The caller treats `None` as "no event
    yet, decide whether to keep waiting"."""
    buf = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except TimeoutError:
            yield None  # soft tick: bound the wait without killing the generator
            continue
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                yield json.loads(line)


def _media_load_args(
    ip: str,
    url: str,
    *,
    title: str = "",
    poster: str = "",
    subtitle: str = "",
    series_title: str = "",
    season: int = 0,
    episode: int = 0,
    content_type: str = "",
    current_time: float = 0.0,
    subtitle_url: str = "",
    subtitle_lang: str = "",
    subtitle_name: str = "",
    app_id: str = "",
) -> dict:
    """Build the `media-load` args, omitting empty optional fields so the daemon picks the
    right metadata block (TvShow if seriesTitle, else Movie if poster/subtitle, else title).
    `subtitle_url` (a WebVTT URL the receiver fetches) adds a side-loaded, auto-activated caption
    track — distinct from `subtitle`, which is Movie-metadata text (a tagline), not a track.
    `app_id` (empty → Default Media Receiver) launches a custom Cast receiver instead — the
    dormant enabling hook for ADR 0013 (a registered receiver with Dolby passthrough); no nstream
    path sets it yet."""
    args: dict = {"ip": ip, "url": url}
    if content_type:
        args["contentType"] = content_type
    if current_time and current_time > 1:
        args["currentTime"] = float(current_time)
    if title:
        args["title"] = title
    if poster:
        args["poster"] = poster
    if subtitle:
        args["subtitle"] = subtitle
    if series_title:
        args["seriesTitle"] = series_title
    if season > 0:
        args["season"] = int(season)
    if episode > 0:
        args["episode"] = int(episode)
    if subtitle_url:
        args["subtitleUrl"] = subtitle_url
        if subtitle_lang:
            args["subtitleLang"] = subtitle_lang
        if subtitle_name:
            args["subtitleName"] = subtitle_name
    if app_id:
        args["appId"] = app_id
    return args


def cast_load(ip: str, url: str, *, follow: bool = True, **meta) -> Generator[dict]:
    """Cast `url` to the receiver at `ip` via castbridge with the given metadata, yielding
    normalized playback events:

        {"kind": "started", "title": str, "tracks": list[int]}
        {"kind": "playing", "position": float, "duration": float, "tracks": list[int]}
        {"kind": "paused",  "position": float, "tracks": list[int]}
        {"kind": "ended",   "position": float, "duration": float}
        {"kind": "failed",  "error": str, "message": str}
        {"kind": "disconnected", "position": float, "duration": float}  # daemon EOF mid-cast

    `tracks` is the receiver's confirmed active track ids (`activeTrackIds`) — for a
    side-loaded caption track it confirms the WebVTT was activated (ADR 0016); empty when the
    receiver doesn't report it. A `failed` with `error: "receiver_error"` is the receiver
    rejecting the media (codec/caption/invalid) mid- or pre-playback.

    `disconnected` (daemon socket EOF after `started`, without an explicit end) means the
    daemon died/restarted — playback on the receiver may well continue; it is not `ended`.

    `meta` accepts title/poster/subtitle/series_title/season/episode/content_type/current_time
    plus subtitle_url/subtitle_lang/subtitle_name (a side-loaded WebVTT caption track).
    With `follow=False` it loads, confirms the handoff, emits `started`, and returns (the daemon
    keeps the session alive). With `follow=True` it streams events until the session ends.
    Never raises: any transport failure becomes a `failed` event so the caller can fall back."""
    if not ensure_daemon():
        yield {
            "kind": "failed",
            "error": "bridge_unavailable",
            "message": "castbridge non disponibile",
        }
        return
    sock = _connect()
    if sock is None:
        yield {
            "kind": "failed",
            "error": "bridge_unavailable",
            "message": "socket castbridge irraggiungibile",
        }
        return

    req_id = 1
    try:
        sock.settimeout(_LOAD_TIMEOUT)
        _send(
            sock, {"id": req_id, "action": "media-load", "args": _media_load_args(ip, url, **meta)}
        )

        started = False
        last_state = ""
        pos = dur = 0.0
        title = str(meta.get("title") or "")
        deadline = time.monotonic() + _LOAD_TIMEOUT
        for msg in _messages(sock):
            if msg is None:
                # Read timeout tick: bound the pre-start wait; once started, keep following.
                if not started and time.monotonic() > deadline:
                    yield {
                        "kind": "failed",
                        "error": "cast_startup_failed",
                        "message": "il cast non è partito",
                    }
                    return
                if started and not follow:
                    break
                continue

            mtype = msg.get("type")
            # Reply to our media-load (carries our id, no "type").
            if mtype is None and msg.get("id") == req_id and msg.get("action") == "media-load":
                if not msg.get("ok"):
                    err = msg.get("error") or {}
                    yield {
                        "kind": "failed",
                        "error": err.get("code", "exec"),
                        "message": err.get("message", "media-load fallito"),
                    }
                    return
                sock.settimeout(_FOLLOW_TIMEOUT)
                continue

            if mtype not in ("media-status", "session"):
                continue  # devices-changed / youtube / mirror events: not ours
            block = _media_block(msg)
            if block is None:  # media session inactive → ended
                if started:
                    yield {"kind": "ended", "position": round(pos, 1), "duration": round(dur, 1)}
                    return
                continue

            # The receiver reported a failure (bad codec, unreachable media, a caption it
            # couldn't fetch — ADR 0016). Before `started` it's a load failure the caller
            # falls back on; after, the session died. Either way, surface it, don't hang.
            err = block.get("error")
            if err:
                yield {
                    "kind": "failed",
                    "error": "receiver_error",
                    "message": str(err),
                }
                return

            state, p, du, t = _progress(block)
            tracks = _active_tracks(block)
            if p:
                pos = p
            if du:
                dur = du
            if t:
                title = t

            if not started and state in ("PLAYING", "PAUSED", "BUFFERING"):
                started = True
                yield {"kind": "started", "title": title, "tracks": tracks}
                if not follow:
                    return
            elif started and state and state != last_state:
                if state == "PAUSED":
                    yield {"kind": "paused", "position": round(pos, 1), "tracks": tracks}
                elif state == "PLAYING":
                    yield {
                        "kind": "playing", "position": round(pos, 1),
                        "duration": round(dur, 1), "tracks": tracks,
                    }  # fmt: skip
            elif started and follow and state == "PLAYING":
                yield {
                    "kind": "playing", "position": round(pos, 1),
                    "duration": round(dur, 1), "tracks": tracks,
                }  # fmt: skip
            last_state = state or last_state

        # Socket closed (EOF) before an explicit end. A daemon crash/restart mid-cast is
        # NOT a playback end: the receiver may still be streaming the media (e.g. from the
        # Tier-2 Range server), so report an honest "disconnected" instead of a fake
        # "ended" — a fake ended would make the caller tear the served file down under a
        # still-playing TV and make the --follow JSONL lie.
        if started:
            yield {"kind": "disconnected", "position": round(pos, 1), "duration": round(dur, 1)}
        elif not follow:
            # Non-follow that never observed a state but loaded ok: best-effort started.
            yield {"kind": "started", "title": title}
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def _media_block(msg: dict) -> dict | None:
    """The active media-status dict from a `media-status`/`session` event, or None when the media
    session is inactive (ended). Callers pre-filter to those two event types. For `media-status`
    the `data` field IS the media block (or null); for `session` it is `data.media` when the
    session is a media session (else mirror/youtube/idle → None)."""
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    if msg.get("type") == "media-status":
        return data
    # session event: only a media session carries our block.
    if data.get("session") != "media":
        return None
    media = data.get("media")
    return media if isinstance(media, dict) else None


def _active_tracks(info: dict) -> list[int]:
    """Track ids the receiver reports active (`activeTrackIds`), ints only — the receiver's
    confirmation of which tracks (incl. a side-loaded caption track) it actually activated
    (ADR 0016). Empty when the receiver doesn't echo it: treated as 'unknown', never a
    downgrade of what was sent."""
    raw = info.get("activeTrackIds")
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, int)]


def _progress(info: dict) -> tuple[str, float, float, str]:
    """Extract (state, position, duration, title) from a media status block, tolerating missing
    fields. Annotated `dict` like `caster._cast_progress` so the JSON access stays type-clean."""
    state = str(info.get("state") or "")
    try:
        pos = float(info.get("position") or 0.0)
    except (TypeError, ValueError):
        pos = 0.0
    try:
        dur = float(info.get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    return state, pos, dur, str(info.get("title") or "")


def _request(action: str, args: dict, *, timeout: float = 8.0) -> dict | None:
    """One-shot request/reply (for stop/status/control). Returns the reply dict, or None on any
    failure. Best-effort, never raises."""
    if not ensure_daemon():
        return None
    sock = _connect()
    if sock is None:
        return None
    try:
        sock.settimeout(timeout)
        _send(sock, {"id": 1, "action": action, "args": args})
        for msg in _messages(sock):
            if msg is None:  # read timeout: give up on this one-shot request
                return None
            if msg.get("id") == 1 and msg.get("action") == action:
                return msg
        return None
    except (TimeoutError, OSError):
        return None
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def stop(ip: str | None = None) -> bool:
    """Stop the active session on the daemon (`stop` action). True on an ok reply."""
    args = {"ip": ip} if ip else {}
    reply = _request("stop", args)
    return bool(reply and reply.get("ok"))


def control(ip: str, cmd: str, value: float = 0.0) -> bool:
    """Send a media-control command (play|pause|seek|volume|mute). True on an ok reply."""
    reply = _request("media-control", {"ip": ip, "cmd": cmd, "value": value})
    return bool(reply and reply.get("ok"))


def status(ip: str | None = None) -> dict | None:
    """Query the daemon's current session (`status` action) → the session data dict, or None."""
    args = {"ip": ip} if ip else {}
    reply = _request("status", args)
    if reply and reply.get("ok"):
        return reply.get("data")
    return None


def peek_status(ip: str | None = None) -> dict | None:
    """Like `status`, but NEVER spawns the daemon: returns None when nothing is already
    listening. For read-only status merges (a `--status` poll) where launching a daemon just
    to answer — when no cast is in progress — would be pointless."""
    sock = _connect()
    if sock is None:
        return None
    sock.close()
    return status(ip)
