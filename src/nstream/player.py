"""Local playback in mpv: launch, position tracking over the IPC socket, and the
mpv.conf / mpv_args inspection that decides which defaults (hwdec, msg-level, alang/
slang, stream-cache) nstream may inject without overriding the user.

Imports nothing from cli (so there's no cycle): cli calls `play()` and the *_defaults
helpers. mpv is a long-lived process driven with subprocess.Popen, so it stays explicit
here rather than going through util.run_cmd.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from importlib import resources

from . import quality
from .config import Config

# IPC socket handshake: mpv creates the socket shortly after launch, so retry a few
# times before giving up. The recv timeout lets the tracker notice mpv exiting promptly
# (so the last observed position isn't lost), and the join timeout bounds shutdown.
_SOCKET_CONNECT_RETRIES = 50
_SOCKET_CONNECT_DELAY = 0.1
_IPC_RECV_TIMEOUT = 0.5
_TRACKER_JOIN_TIMEOUT = 2.0


def _track_position(
    sock_path: str,
    holder: dict[str, float],
    proc: subprocess.Popen,
) -> None:
    """Observe mpv's time-pos/duration over the IPC socket; record the last values."""
    sock: socket.socket | None = None
    for _ in range(_SOCKET_CONNECT_RETRIES):
        if proc.poll() is not None:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(sock_path)
            break
        except OSError:
            sock = None
            time.sleep(_SOCKET_CONNECT_DELAY)
    if sock is None:
        return
    try:
        for cid, prop in ((1, "time-pos"), (2, "duration")):
            sock.sendall(f'{{"command":["observe_property",{cid},"{prop}"]}}\n'.encode())
        # Time out recv so we notice mpv exiting promptly and the thread joins
        # cleanly — otherwise a blocked recv could outlive mpv and lose the last
        # observed position.
        sock.settimeout(_IPC_RECV_TIMEOUT)
        buf = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                if proc.poll() is not None:
                    break
                continue
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


def _mpv_conf_lines() -> Iterator[str]:
    """Yield the non-empty, non-comment lines of the user's mpv.conf (first found).
    Shared by `_mpv_conf_has`/`_mpv_conf_get` so the file walk lives in one place."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    candidates = (
        os.path.join(base, "mpv", "mpv.conf"),
        os.path.expanduser("~/.mpv/mpv.conf"),
    )
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines:
            s = line.strip()
            if s and not s.startswith("#"):
                yield s


def _mpv_conf_has(option: str) -> bool:
    """True if the user's mpv.conf sets `option` (including as a bare flag line),
    so nstream must not override it."""
    return any(s.split("=", 1)[0].strip() == option for s in _mpv_conf_lines())


def _mpv_conf_get(option: str) -> str | None:
    """The value the user's mpv.conf sets for `option` (a `key=value` line), or None.
    Quotes are stripped so `hwdec="auto"` and `hwdec=auto` read the same."""
    for s in _mpv_conf_lines():
        if "=" in s:
            key, _, val = s.partition("=")
            if key.strip() == option:
                return val.strip().strip("'\"")
    return None


def _user_overrides(cfg: Config, option: str) -> bool:
    """True if the user already sets `option` via mpv_args or mpv.conf."""
    return any(a.startswith(f"--{option}") for a in cfg.mpv_args) or _mpv_conf_has(option)


# mpv's "auto" hwdec family: ambiguous choices that probe several methods. On a
# vulkan render context mpv 0.41+ prefers (experimental, often unsupported) Vulkan
# decode and then CUDA before VAAPI — noisy and software-bound on Intel iGPUs.
_AUTO_HWDEC = {"auto", "auto-safe", "auto-copy", "auto-safe-copy"}


def _hwdec_defaults(cfg: Config) -> list[str]:
    """Choose mpv's hwdec, upgrading the ambiguous `auto` family to the GPU's real
    method (VAAPI, detected) so mpv doesn't probe unsupported Vulkan decode / missing
    CUDA. An explicit `--hwdec` in mpv_args, or a concrete method in mpv.conf, is
    always respected; nstream's CLI flag overrides mpv.conf only to pin `auto`→vaapi."""
    if any(a.startswith("--hwdec") for a in cfg.mpv_args):
        return []  # explicit per-app override → defer entirely
    conf = _mpv_conf_get("hwdec")
    method = conf if conf is not None else cfg.hwdec
    if not method:
        return []  # decoding left to mpv defaults / explicitly disabled
    if method in _AUTO_HWDEC:
        detected = quality.preferred_hwdec(quality.detect_caps())
        if detected:
            return [f"--hwdec={detected}"]
        return [] if conf is not None else [f"--hwdec={method}"]
    # Concrete method: respect mpv.conf as-is; inject only nstream's own config value.
    return [] if conf is not None else [f"--hwdec={method}"]


# mpv log modules that spam the terminal of a media frontend (track list +
# decoder/demuxer/driver warnings). cplayer=warn drops the track list while the
# progress status line (a separate mechanism) survives; the rest hide ffmpeg/mkv
# warnings. Real errors (level error/fatal) still print.
_QUIET_MSG_LEVEL = (
    "cplayer=warn,ffmpeg=error,ffmpeg/video=error,ffmpeg/audio=error,mkv=error,ad=error,vd=error"  # noqa: E501
)


def _quiet_defaults(cfg: Config) -> list[str]:
    """Quieten mpv's console unless the user manages msg-level themselves."""
    if not cfg.mpv_quiet or _user_overrides(cfg, "msg-level"):
        return []
    return [f"--msg-level={_QUIET_MSG_LEVEL}"]


def _display_tags_defaults(cfg: Config) -> list[str]:
    """Suppress mpv's terminal "File tags:" block (Date/Description/Title) unless the user
    manages display-tags themselves. Those are the *file's* embedded container metadata —
    often a wrong-language plot baked into the release — and pure noise for a frontend that
    already forces the correct title via --force-media-title. (--msg-level can't hide this
    block; --display-tags is the only lever.) Gated by mpv_quiet, like the console quieting."""
    if not cfg.mpv_quiet or _user_overrides(cfg, "display-tags"):
        return []
    return ["--display-tags="]


def _stream_cache_defaults(cfg: Config) -> list[str]:
    """Anti-desync defaults for network playback (every url nstream plays is one):
    a bigger demuxer readahead bound (mpv's default 150MiB is ~15s of a 4K remux) and
    pause-to-rebuffer at start/seek/underrun instead of letting A/V drift. Each flag
    is skipped when the user manages that option; a user-managed cache-pause defers
    the whole pause family (the mpv_args prefix check makes any --cache-pause-* there
    trip the family gate too — over-conservative on purpose)."""
    flags = []
    if not _user_overrides(cfg, "demuxer-max-bytes"):
        flags.append("--demuxer-max-bytes=512MiB")
    if not _user_overrides(cfg, "cache-pause"):
        if not _user_overrides(cfg, "cache-pause-initial"):
            flags.append("--cache-pause-initial=yes")
        if not _user_overrides(cfg, "cache-pause-wait"):
            flags.append("--cache-pause-wait=3")
    return flags


def _lang_defaults(cfg: Config, *, audio_lang: str | None = None) -> list[str]:
    """Prefer the user's languages for audio/subtitle track selection, without
    overriding any alang/slang the user already set. `--subs-with-matching-audio=no`
    means: don't force subtitles on when the audio is already in your language.

    `audio_lang` (per-invocation `--audio-lang`) is put first in `--alang` so mpv picks
    that dub when the container has multiple tracks."""
    flags = []
    if not _user_overrides(cfg, "alang"):
        if audio_lang:
            rest = [c for c in cfg.audio_langs if c != audio_lang]
            flags.append("--alang=" + ",".join([audio_lang, *rest]))
        elif cfg.audio_langs:
            flags.append("--alang=" + ",".join(cfg.audio_langs))
    if cfg.subtitle_langs and not _user_overrides(cfg, "slang"):
        flags.append("--slang=" + ",".join(cfg.subtitle_langs))
        if not _user_overrides(cfg, "subs-with-matching-audio"):
            flags.append("--subs-with-matching-audio=no")
    return flags


def play(
    cfg: Config,
    title: str,
    url: str,
    *,
    start: float | None = None,
    sub_paths: tuple[str, ...] = (),
    audio_id: int | None = None,
    sub_id: int | str | None = None,
    next_label: str | None = None,
    cast_enabled: bool = False,
    work_dir: str | None = None,
    audio_lang: str | None = None,
) -> tuple[float, float, str]:
    """Play `url` in mpv. Returns (position, duration, signal) where `signal` is
    "next" when the next-episode overlay asked to continue, "cast" when the user hit
    the in-player "send to TV" key (Alt-C), or "" otherwise.

    `work_dir` holds the IPC socket and overlay files; when omitted a private
    temp dir is created and removed here (the caller passes one to share it with
    downloaded subtitles). `cast_enabled` binds Alt-C in mpv to move playback to the TV."""
    holder = {"position": 0.0, "duration": 0.0}
    with contextlib.ExitStack() as stack:
        if work_dir is None:
            runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
            work_dir = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="nstream-", dir=runtime)
            )
        sock_path = os.path.join(work_dir, "mpv.sock")
        signal_path = os.path.join(work_dir, "signal")
        info_path = os.path.join(work_dir, "info")
        # Defaults come before mpv_args so explicit user flags win. nstream is the
        # single source of truth for resume (via --start over IPC), so
        # --no-resume-playback stops mpv's own watch-later from doing a second,
        # conflicting seek.
        args = [
            "mpv",
            f"--force-media-title={title}",
            "--no-resume-playback",
            *_quiet_defaults(cfg),
            *_display_tags_defaults(cfg),
            *_hwdec_defaults(cfg),
            *_lang_defaults(cfg, audio_lang=audio_lang),
            *_stream_cache_defaults(cfg),
            *cfg.mpv_args,
        ]
        if start and start > 1:
            args.append(f"--start={start:.0f}")
        args += [f"--sub-file={p}" for p in sub_paths]
        # Explicit per-play track choices win over the language-preference defaults.
        if audio_id is not None:
            args.append(f"--aid={audio_id}")
        if sub_id is not None:
            args.append(f"--sid={sub_id}")
        args.append(f"--input-ipc-server={sock_path}")

        # The overlay script is the single renderer for both the resume toast and the
        # next-episode card, so load it always (it's additive — it never touches the
        # user's mpv.conf). --script-opts-append is non-destructive: it won't clobber
        # a user's own script-opts set for other scripts.
        lua = stack.enter_context(resources.as_file(resources.files("nstream") / "nstream.lua"))
        args.append(f"--script={lua}")
        if start and start > 1:
            args.append(f"--script-opts-append=nstream-resume={start:.0f}")
        # The next-episode card and the in-player "send to TV" key both report back via
        # the same signal file. Pass it whenever either feature is active.
        if next_label:
            with open(info_path, "w", encoding="utf-8") as f:
                f.write(next_label + "\n")
            args += [
                f"--script-opts-append=nstream-info={info_path}",
                f"--script-opts-append=nstream-lead={cfg.autoplay_lead}",
            ]
        if next_label or cast_enabled:
            args.append(f"--script-opts-append=nstream-signal={signal_path}")
        if cast_enabled:
            args.append("--script-opts-append=nstream-cast=yes")
        args.append(url)
        try:
            proc = subprocess.Popen(args)
        except FileNotFoundError:
            print("nstream: mpv non trovato", file=sys.stderr)
            return (0.0, 0.0, "")
        tracker = threading.Thread(
            target=_track_position, args=(sock_path, holder, proc), daemon=True
        )
        tracker.start()
        proc.wait()
        tracker.join(timeout=_TRACKER_JOIN_TIMEOUT)
        signal = ""
        if next_label or cast_enabled:
            with contextlib.suppress(OSError), open(signal_path, encoding="utf-8") as f:
                signal = f.read().strip()

    return (holder["position"], holder["duration"], signal)
