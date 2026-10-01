"""Domain notices reach --json (ADR 0037) while stderr stays byte-identical."""

from __future__ import annotations

import json

from nstream import log, notices


def test_emit_prints_identically_and_captures(capsys):
    with notices.capture() as bag:
        notices.emit("audio ita non disponibile", code="audio_lang_absent")
        notices.emit("x", render="nstream: ⚠ x")
    err = capsys.readouterr().err
    assert err == "nstream: audio ita non disponibile\nnstream: ⚠ x\n"
    assert [n.code for n in bag] == ["audio_lang_absent", ""]


def test_emit_outside_capture_only_prints(capsys):
    notices.emit("solo stderr")
    assert notices.collected() is None
    assert capsys.readouterr().err == "nstream: solo stderr\n"


def test_json_result_carries_notices_but_events_do_not(capsys):
    with notices.capture():
        notices.emit("nessuna VPN", code="p2p_no_vpn")
        log.emit_json({"ok": True, "action": "cast", "event": "playing"})
        log.emit_json({"ok": True, "action": "cast"})
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert "notices" not in lines[0]
    assert lines[1]["notices"] == [{"text": "nessuna VPN", "code": "p2p_no_vpn", "level": "warn"}]
