"""Transactional playback transfer; destination readiness precedes source retirement.

The worker owns destination resources. Only descriptive media identity/preferences cross
its private pipe; sources are re-resolved through the normal, privacy-gated play flow.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable

from . import cast_delivery, caster, config, headless_play, local_control, log, remux, state, util
from .config import Config, PlayOpts
from .state import playback_session

_READY_TIMEOUT = 240.0
_STATE_TIMEOUT = 12.0


class TransferError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def current(target: str) -> dict | None:
    return playback_session.local_info() if target == "local" else state.active_cast_info()


def source(identity: str) -> tuple[str, dict]:
    for target in ("local", "cast"):
        value = current(target)
        if value and value.get("session_id") == identity:
            if not playback_session.transferable(value):
                raise TransferError("session_not_transferable")
            return target, value
    raise TransferError("session_changed")


class Endpoint:
    def __init__(self, target: str, session: dict):
        self.target = target
        self.session = session
        self.fingerprint: str | None = session.get("transfer_fingerprint")
        legacy = state.cast_session_info() if target == "cast" else None
        self.legacy = (
            legacy
            if (
                legacy
                and legacy.get("device") == session.get("device")
                and legacy.get("video_id") == session.get("video_id")
            )
            else None
        )
        self.delivery = remux.delivery_identity(session.get("device")) if target == "cast" else None

    def check(self) -> None:
        value = current(self.target)
        if not value or value.get("session_id") != self.session.get("session_id"):
            raise TransferError("session_changed")

    def snapshot(self) -> dict:
        self.check()
        if self.target == "local":
            try:
                return local_control.snapshot(self.session)
            except local_control.LocalUnavailable:
                raise TransferError("source_unavailable") from None
        info = caster.receiver_info(self.session["device"])
        position, duration, player_state = caster._cast_progress(info)
        if player_state not in ("PLAYING", "PAUSED", "BUFFERING"):
            raise TransferError("source_unavailable")
        bound = (current("cast") or {}).get("receiver_session_id")
        if bound is not None and bound != info.get("media_session_id"):
            raise TransferError("session_changed")
        if not all(math.isfinite(v) and v >= 0 for v in (position, duration)):
            raise TransferError("source_not_ready")
        # Pin the physical receiver generation for this transaction; never expose its URL.
        identity = [info.get(k) for k in ("app_id", "session_id", "media_session_id", "content_id")]
        if not identity[-1] or identity[2] is None:
            raise TransferError("receiver_identity_unavailable")
        fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if self.fingerprint and fingerprint != self.fingerprint:
            raise TransferError("session_changed")
        self.fingerprint = fingerprint
        if (
            self.session.get("handoff_id")
            and self.session.get("transfer_fingerprint") != fingerprint
        ):
            playback_session.stamp_cast(self.session["session_id"], fingerprint)
            self.session["transfer_fingerprint"] = fingerprint
        title = (info.get("media_metadata") or {}).get("title") or ""
        from .state.cast_session import _session_title_matches

        if title and not _session_title_matches(self.session["title"], title):
            raise TransferError("session_changed")
        return {
            "position": position,
            "duration": duration
            if duration > 0
            else cast_delivery.live_duration(self.session["device"]),
            "player_state": player_state,
        }

    def control(self, action: str, value: float = 0) -> None:
        self.check()
        if self.target == "local":
            args = (
                ["seek", value, "absolute+exact"]
                if action == "seek"
                else ["set_property", "pause", action == "pause"]
            )
            try:
                local_control.command(self.session, args)
            except local_control.LocalUnavailable:
                raise TransferError("control_failed") from None
        else:
            self.snapshot()  # A receiver replaced outside nstream must not receive our command.
            from . import cast_control

            ok, _ = cast_control.media_control(action, value=value, device=self.session["device"])
            if not ok:
                raise TransferError("control_failed")
            if action == "seek":
                # A legitimate HLS seek may create a new playlist/media generation.
                self.fingerprint = None

    def stop(self) -> None:
        self.check()
        if self.target == "local":
            try:
                local_control.command(self.session, ["quit"])
            except local_control.LocalUnavailable:
                if util.pid_alive(self.session.get("pid")):
                    raise TransferError("source_stop_failed") from None
            deadline = time.monotonic() + 3
            while util.pid_alive(self.session.get("pid")) and time.monotonic() < deadline:
                time.sleep(0.05)
            if util.pid_alive(self.session.get("pid")):
                raise TransferError("source_stop_failed")
            playback_session.clear_local(self.session["session_id"])
        else:
            # Recheck physical identity immediately before the destructive operation.
            self.snapshot()
            if not caster.stop(self.session["device"]):
                raise TransferError("source_stop_failed")
            self.check_or_gone()
            # Destination local playback resolved its own source: it does not borrow HLS.
            remux.retire_delivery(self.delivery)
            state.clear_active_cast(session_id=self.session["session_id"])
            if self.legacy:
                state.clear_cast_session(expected=self.legacy)

    def check_or_gone(self) -> None:
        value = current(self.target)
        if value and value.get("session_id") != self.session.get("session_id"):
            raise TransferError("session_changed")


def await_state(endpoint: Endpoint, predicate: Callable[[dict], bool]) -> dict:
    deadline = time.monotonic() + _STATE_TIMEOUT
    while True:
        sample = endpoint.snapshot()
        if predicate(sample):
            return sample
        if time.monotonic() >= deadline:
            raise TransferError("target_not_ready")
        time.sleep(0.2)


def commit(origin: Endpoint, destination: Endpoint, *, initial: dict) -> dict:
    """Pause only after target readiness, align its cursor, then retire the same origin."""
    paused_origin = False
    retiring_origin = False
    try:
        ready = await_state(destination, lambda s: s["player_state"] in ("PLAYING", "PAUSED"))
        origin.check()
        live = origin.snapshot()
        if not (live["duration"] > 0 and ready["duration"] > 0):
            raise TransferError("duration_unverified")
        if abs(live["duration"] - ready["duration"]) > max(5, live["duration"] * 0.001):
            raise TransferError("edition_mismatch")
        destination.control("pause")
        await_state(destination, lambda s: s["player_state"] == "PAUSED")
        if live["player_state"] != "PAUSED":
            origin.control("pause")
            paused_origin = True
        final = await_state(origin, lambda s: s["player_state"] == "PAUSED")
        position = final["position"]
        destination.control("seek", position)
        await_state(destination, lambda s: abs(s["position"] - position) <= 3)
        desired = live["player_state"]
        destination.control("pause" if desired == "PAUSED" else "play")
        await_state(
            destination,
            lambda s: s["player_state"] == desired and abs(s["position"] - position) <= 5,
        )
        origin.check()
        destination.check()
        retiring_origin = True
        origin.stop()
        return {
            "ok": True,
            "action": "transfer",
            "target": destination.target,
            "session_id": destination.session["session_id"],
            "position": round(position, 2),
            "source_stopped": True,
            "error": None,
        }
    except (TransferError, local_control.LocalUnavailable):
        # Roll back only the destination we own. A replacement session is never stopped.
        active = current(origin.target)
        if active is None:
            if retiring_origin:
                raise TransferError("transfer_incomplete") from None
            with contextlib.suppress(TransferError):
                destination.stop()
            raise TransferError("source_ended") from None
        same = active.get("session_id") == origin.session.get("session_id")
        restored = not paused_origin or not same
        if paused_origin and same:
            with contextlib.suppress(TransferError):
                origin.control("play")
                await_state(origin, lambda s: s["player_state"] == "PLAYING")
                restored = True
        if restored:
            with contextlib.suppress(TransferError):
                destination.stop()
        # If the source disappeared, keep the verified destination available instead of
        # leaving both endpoints stopped. The caller reports this as incomplete.
        if not restored:
            raise TransferError("transfer_incomplete") from None
        raise


def _spawn(request: dict) -> tuple[subprocess.Popen, int]:
    read_fd, write_fd = os.pipe()
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "nstream.handoff", "--reply-fd", str(write_fd)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(write_fd,),
        )
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(request) + "\n").encode())
        proc.stdin.close()
    except BaseException:
        os.close(read_fd)
        if proc is not None:
            _terminate_worker(proc)
        raise
    finally:
        os.close(write_fd)
    return proc, read_fd


def _terminate_worker(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _rollback_worker(
    target: str, operation: str, proc: subprocess.Popen, endpoint: Endpoint | None = None
) -> None:
    value = current(target)
    if value and value.get("handoff_id") == operation:
        owned = endpoint or Endpoint(target, value)
        if target == "local" or owned.fingerprint:
            with contextlib.suppress(TransferError):
                owned.stop()
    _terminate_worker(proc)


def transfer(cfg: Config, identity: str, target: str, device: str | None = None) -> dict:
    proc = None
    destination = None
    read_fd = None
    operation = uuid.uuid4().hex
    try:
        if target not in ("local", "cast") or not identity:
            raise TransferError("usage")
        with util.file_lock(util.runtime_dir() / "nstream-transfer.json", timeout=0.1):
            kind, record = source(identity)
            if kind == target:
                raise TransferError("same_target")
            if current(target):
                raise TransferError("target_busy")
            if target == "cast":
                device = caster.resolve_device(cfg, headless=True, prefer=device)
                receiver = caster.receiver_info(device)
                if not receiver:
                    raise TransferError("target_unavailable")
                if receiver.get("player_state") in ("PLAYING", "PAUSED", "BUFFERING"):
                    raise TransferError("target_busy")
            origin = Endpoint(kind, record)
            initial = origin.snapshot()
            if initial["player_state"] not in ("PLAYING", "PAUSED"):
                raise TransferError("source_not_ready")
            prefs = (
                local_control.preferences(record)
                if kind == "local"
                else record.get("preferences", {})
            )
            request = {
                "target": target,
                "device": device,
                "operation": operation,
                "type": record["type"],
                "video_id": record["video_id"],
                "title": record["title"],
                "preferences": prefs,
                "release": record["release"],
                "start": initial["position"],
            }
            proc, read_fd = _spawn(request)
            if not select.select([read_fd], [], [], _READY_TIMEOUT)[0]:
                raise TransferError("target_timeout")
            reply = json.loads(os.read(read_fd, 8192))
            if not isinstance(reply, dict) or not reply.get("ok"):
                raise TransferError(
                    str(reply.get("error") or "target_failed")
                    if isinstance(reply, dict)
                    else "target_failed"
                )
            value = current(target)
            if (
                not value
                or value.get("session_id") != reply.get("session_id")
                or value.get("handoff_id") != operation
            ):
                raise TransferError("session_changed")
            destination = Endpoint(target, value)
            result = commit(origin, destination, initial=initial)
            return {**result, "title": record["title"]}
    except (
        TransferError,
        caster.CastUnavailable,
        OSError,
        ValueError,
        TimeoutError,
        local_control.LocalUnavailable,
    ) as exc:
        code = (
            exc.code
            if isinstance(exc, TransferError)
            else "transfer_busy"
            if isinstance(exc, TimeoutError)
            else "transfer_failed"
        )
        if proc is not None and code != "transfer_incomplete":
            _rollback_worker(target, operation, proc, destination)
        return {
            "ok": False,
            "action": "transfer",
            "error": code,
            "message": "trasferimento non completato; controlla lo stato dei player",
        }
    finally:
        if read_fd is not None:
            os.close(read_fd)


def worker(reply_fd: int) -> int:
    sent = False

    def reply(result: dict) -> None:
        nonlocal sent
        if sent:
            return
        sent = True
        try:
            os.write(reply_fd, (json.dumps(result) + "\n").encode())
        except OSError:
            pass
        finally:
            os.close(reply_fd)

    def ready(value: dict) -> None:
        if request["target"] == "cast":
            endpoint = Endpoint("cast", value)
            endpoint.snapshot()
            if endpoint.fingerprint:
                playback_session.stamp_cast(value["session_id"], endpoint.fingerprint)
        reply({"ok": True, "session_id": value["session_id"]})

    def terminate(signum, frame):
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, terminate)
    try:
        request = json.loads(sys.stdin.buffer.readline(16385))
        if (
            not isinstance(request, dict)
            or request.get("type") not in ("movie", "series")
            or not re.fullmatch(r"[\w:.-]{1,200}", str(request.get("video_id") or ""))
            or request.get("target") not in ("local", "cast")
            or not re.fullmatch(r"[a-f0-9]{32}", str(request.get("operation") or ""))
        ):
            raise TransferError("usage")
        start = float(request["start"])
        if not math.isfinite(start) or start < 0:
            raise TransferError("usage")
        cfg = config.load()
        prefs = request.get("preferences") or {}
        opts = PlayOpts(
            auto=True,
            cast=request["target"] == "cast",
            sub_mode=prefs.get("sub_mode"),
            sub_lang=prefs.get("sub_lang"),
            history=bool(prefs.get("history")),
            autoplay=False,
            audio_lang=prefs.get("audio_lang"),
            quality=prefs.get("quality"),
            mirror=False,
            sub_offset=float(prefs.get("sub_offset") or 0),
            sub_scale=float(prefs.get("sub_scale") or 1),
        )
        args = argparse.Namespace(device=request.get("device"), follow=False, volume=None)
        season = episode = None
        parts = request["video_id"].rsplit(":", 2)
        if (
            request["type"] == "series"
            and len(parts) == 3
            and all(part.isdecimal() for part in parts[1:])
        ):
            season, episode = int(parts[1]), int(parts[2])
        token = playback_session.operation_id.set(request["operation"])
        try:
            # The ordinary play use case owns temp files, subtitle policy and P2P gates.
            with tempfile.TemporaryFile(mode="w+") as output, contextlib.redirect_stdout(output):
                rc = headless_play.auto_play(
                    cfg,
                    args,
                    opts,
                    request["type"],
                    request["video_id"],
                    str(request["title"])[:200],
                    request["video_id"].split(":")[0],
                    season,
                    episode,
                    "transfer",
                    start_override=start,
                    on_started=ready,
                    release=request.get("release"),
                )
                if not sent:
                    output.seek(0)
                    lines = output.read(65536).splitlines()
                    code = "target_failed"
                    if lines:
                        with contextlib.suppress(ValueError, AttributeError):
                            candidate = json.loads(lines[-1]).get("error")
                            if isinstance(candidate, str) and re.fullmatch(
                                r"[a-z_]{1,64}", candidate
                            ):
                                code = candidate
                    reply({"ok": False, "error": code})
            return rc
        finally:
            playback_session.operation_id.reset(token)
    except Exception:
        reply({"ok": False, "error": "target_failed"})
        return 1
    finally:
        if not sent:
            os.close(reply_fd)


def transfer_to_local(cfg: Config, *, device: str | None = None) -> tuple[bool, str]:
    """Move the exact owned cast using the public core transfer transaction."""
    from .state import playback_session

    active = state.active_cast_info()
    if (
        not playback_session.transferable(active)
        or active is None
        or (device and active.get("device") != device)
    ):
        return False, "Trasferimento non disponibile per questa riproduzione"
    result = transfer(cfg, active["session_id"], "local")
    return bool(result["ok"]), (
        "Riproduzione spostata sul computer"
        if result["ok"]
        else f"Trasferimento non completato ({result['error']})"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reply-fd", type=int, required=True)
    log.setup_logging(False)
    raise SystemExit(worker(parser.parse_args().reply_fd))
