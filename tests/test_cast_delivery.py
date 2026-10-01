"""Unit tests for the shared castbridge delivery driver (ADR 0011)."""

from __future__ import annotations

import pytest

from nstream import cast_delivery


def _drive(monkeypatch, events, **hooks):
    def fake_load(ip, url, *, follow=True, **kwargs):
        yield from events

    monkeypatch.setattr(cast_delivery.bridge, "cast_load", fake_load)
    return cast_delivery.drive_bridge("1.2.3.4", "http://u", follow=True, load_kwargs={}, **hooks)


def test_failed_before_started_returns_none(monkeypatch):
    out = _drive(monkeypatch, [{"kind": "failed", "error": "load_failed", "message": "x"}])
    assert out is None  # caller falls back to catt


def test_receiver_error_before_started_is_not_a_fallback(monkeypatch):
    # The TV refused the media: catt would load the same url and its exit code would read
    # as a start (false `ok: true`). Report a not-started outcome instead.
    out = _drive(monkeypatch, [{"kind": "failed", "error": "receiver_error", "message": "x"}])
    assert out is not None and not out.started
    assert out.error == "receiver_error"


def test_failed_after_started_is_no_fallback(monkeypatch):
    out = _drive(
        monkeypatch,
        [
            {"kind": "started"},
            {"kind": "playing", "position": 10.0, "duration": 100.0},
            {"kind": "failed", "error": "media_error"},
        ],
    )
    assert out is not None and out.started  # catt wouldn't fare better: no fallback
    assert (out.pos, out.dur) == (10.0, 100.0)


def test_tracks_position_and_confirmed_tracks(monkeypatch):
    out = _drive(
        monkeypatch,
        [
            {"kind": "started", "tracks": [1]},
            {"kind": "playing", "position": 50.0, "duration": 100.0, "tracks": []},
            {"kind": "ended", "position": 98.0, "duration": 100.0},
        ],
    )
    # an empty echo never erases a confirmation already seen
    assert out == cast_delivery.BridgeOutcome(98.0, 100.0, True, False, tracks=(1,))


@pytest.mark.parametrize(
    ("kwargs", "tracks", "expected"),
    [
        ({}, (), False),  # nothing side-loaded
        ({"subtitle_url": "u"}, (), True),  # receiver reports nothing: unknown, not a downgrade
        ({"subtitle_url": "u"}, (1,), True),  # confirmed
        ({"subtitle_url": "u"}, (2,), False),  # receiver activated something else
    ],
)
def test_caption_active(kwargs, tracks, expected):
    assert cast_delivery.caption_active(kwargs, tracks) is expected


def test_on_started_fires_once(monkeypatch):
    fired = []
    out = _drive(
        monkeypatch,
        [{"kind": "started"}, {"kind": "started"}, {"kind": "playing", "position": 1.0}],
        on_started=lambda: fired.append(1),
    )
    assert out is not None and out.started and fired == [1]


def test_events_forwarded_in_order(monkeypatch):
    seen = []
    events = [
        {"kind": "started"},
        {"kind": "playing", "position": 1.0, "duration": 2.0},
        {"kind": "ended", "position": 2.0, "duration": 2.0},
    ]
    _drive(monkeypatch, events, on_event=seen.append)
    assert [e["kind"] for e in seen] == ["started", "playing", "ended"]


def test_disconnect_hook_breaks_and_flags(monkeypatch):
    told = []
    out = _drive(
        monkeypatch,
        [
            {"kind": "started"},
            {"kind": "playing", "position": 30.0, "duration": 100.0},
            {"kind": "disconnected", "position": 42.0},
            {"kind": "ended", "position": 99.0, "duration": 100.0},  # never consumed
        ],
        on_disconnect=told.append,
    )
    assert out is not None and out.disconnected and out.pos == 42.0
    assert told == [42.0]
    assert out.pos != 99.0  # the ended event after the break was not consumed


def test_disconnect_without_hook_keeps_following(monkeypatch):
    out = _drive(
        monkeypatch,
        [
            {"kind": "started"},
            {"kind": "disconnected", "position": 42.0, "duration": 100.0},
            {"kind": "ended", "position": 99.0, "duration": 100.0},
        ],
    )
    assert out is not None and out.disconnected is False  # stream left to end on its own
    assert out.pos == 99.0 and cast_delivery.is_finished(out.pos, out.dur)


def _interrupting_load(events_before_interrupt):
    def fake_load(ip, url, *, follow=True, **kwargs):
        yield from events_before_interrupt
        raise KeyboardInterrupt

    return fake_load


def test_interrupt_default_swallows_and_stops_receiver(monkeypatch):
    stopped = []
    monkeypatch.setattr(cast_delivery.bridge, "stop", lambda ip: stopped.append(ip))
    monkeypatch.setattr(
        cast_delivery.bridge,
        "cast_load",
        _interrupting_load([{"kind": "started"}, {"kind": "playing", "position": 9.0}]),
    )
    out = cast_delivery.drive_bridge("1.2.3.4", "http://u", follow=True, load_kwargs={})
    assert out is not None and out.pos == 9.0  # interactive: stop following, keep result
    assert stopped == ["1.2.3.4"]


def test_interrupt_hook_decides_reraise(monkeypatch):
    monkeypatch.setattr(cast_delivery.bridge, "stop", lambda ip: None)
    monkeypatch.setattr(cast_delivery.bridge, "cast_load", _interrupting_load([]))
    asked = []

    def abort(started: bool) -> bool:
        asked.append(started)
        return True

    with pytest.raises(KeyboardInterrupt):
        cast_delivery.drive_bridge(
            "1.2.3.4", "http://u", follow=True, load_kwargs={}, on_interrupt=abort
        )
    assert asked == [False]  # the hook sees whether started had happened


# --- the single finish predicate (ADR 0029) ---------------------------------


@pytest.mark.parametrize(
    ("pos", "dur", "expected"),
    [
        (97.0, 100.0, True),  # exactly at CAST_DONE
        (96.9, 100.0, False),  # just under
        (100.0, 100.0, True),
        (50.0, 100.0, False),  # manual stop mid-episode must not binge ahead
        (0.0, 0.0, False),  # unobserved position (fire-and-return) is never a finish
        (98.0, 0.0, False),  # position without a duration proves nothing
    ],
)
def test_is_finished(pos, dur, expected):
    assert cast_delivery.is_finished(pos, dur) is expected
