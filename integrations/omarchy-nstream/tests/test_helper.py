"""Unit tests for helper.py (stdlib only: `python3 -m unittest discover -s tests`)."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import helper  # noqa: E402  # ty: ignore[import-not-found]


def call(*argv: str, results: list[dict] | None = None) -> tuple[dict, list[list[str]]]:
    """Run helper.main with `nstream` stubbed; return its JSON and the nstream argvs."""
    calls: list[list[str]] = []
    queue = list(results or [{"ok": True}])

    def fake_run(args, timeout):
        calls.append(list(args))
        return queue.pop(0) if queue else {"ok": True}

    out = io.StringIO()
    with mock.patch.object(helper, "run", fake_run), contextlib.redirect_stdout(out):
        helper.main(list(argv))
    return json.loads(out.getvalue()), calls


class HistoryTest(unittest.TestCase):
    def test_bounds_and_sanitizes_entries(self):
        raw = {
            "ok": True,
            "entries": [
                {
                    "title": "Show\x1b[31m\n",
                    "type": "series",
                    "season": 1,
                    "episode": 2,
                    "position": 10,
                    "duration": 20,
                    "next": True,
                },
                {"title": "", "type": "movie"},
                {"title": "x" * 500, "type": "weird", "season": "1", "position": "nan"},
            ],
            "cast_session": {"title": "Show", "device": "TV"},
        }
        with mock.patch.object(helper, "prefer_cast", return_value=True):
            out, calls = call("history", results=[raw])
        self.assertEqual(calls, [["--history"]])
        self.assertTrue(out["ok"] and out["preferCast"])
        show, film = out["entries"]
        self.assertEqual(show["title"], "Show[31m")
        self.assertEqual((show["season"], show["episode"], show["next"]), (1, 2, True))
        self.assertEqual(len(film["title"]), helper.MAX_TITLE)
        self.assertEqual((film["type"], film["season"], film["position"]), ("movie", 0, 0.0))
        self.assertEqual(out["session"], {"title": "Show", "device": "TV"})

    def test_failure_is_reported(self):
        out, _ = call("history", results=[{"ok": False, "error": "x", "message": "rotto"}])
        self.assertEqual(out, {"kind": "history", "ok": False, "error": "rotto"})


class ControlTest(unittest.TestCase):
    def test_status_fields(self):
        st = {
            "ok": True,
            "device": "TV",
            "title": "Dune",
            "player_state": "playing",
            "position": 12.5,
            "duration": 100,
            "volume_percent": 140,
        }
        out, _ = call("status", results=[st])
        self.assertEqual((out["state"], out["volume"], out["position"]), ("PLAYING", 100, 12.5))
        out, _ = call("status", results=[{"ok": True, "player_state": "IDLE"}])
        self.assertEqual(out["volume"], -1)

    def test_relative_seek_clamps_to_duration(self):
        _, calls = call("seek", "30", results=[{"ok": True, "position": 90, "duration": 100}])
        self.assertEqual(calls, [["--status"], ["--seek", "95"]])
        _, calls = call("seek", "-30", results=[{"ok": True, "position": 10, "duration": 100}])
        self.assertEqual(calls[-1], ["--seek", "0"])

    def test_rejects_out_of_range_values(self):
        for argv in (
            ("volume", "101"),
            ("volume", "x"),
            ("subshift", "31"),
            ("seekto", "-1"),
            ("seek", "1e9"),
            ("pause", "extra"),
            ("rm", "-rf"),
        ):
            out, calls = call(*argv)
            self.assertFalse(out["ok"], argv)
            self.assertEqual(calls, [], argv)

    def test_simple_actions_map_to_flags(self):
        for cmd in ("pause", "resume", "stop"):
            _, calls = call(cmd)
            self.assertEqual(calls, [["--" + cmd]])
        _, calls = call("volume", "40")
        self.assertEqual(calls, [["--volume", "40"]])
        _, calls = call("subshift", "-0.5")
        self.assertEqual(calls, [["--sub-shift", "-0.5"]])


class ContinueTest(unittest.TestCase):
    def test_cast_passes_title_after_separator(self):
        out, calls = call("continue", "cast", "--stop", results=[{"ok": True, "title": "T"}])
        self.assertEqual(calls, [["-c", "--cast", "--", "--stop"]])
        self.assertTrue(out["ok"])

    def test_local_is_detached(self):
        out = io.StringIO()
        with (
            mock.patch.object(helper, "nstream_bin", return_value="/bin/nstream"),
            mock.patch.object(helper.subprocess, "Popen") as popen,
            contextlib.redirect_stdout(out),
        ):
            helper.main(["continue", "local", "Dune"])
        argv = popen.call_args.args[0]
        self.assertEqual(argv, ["/bin/nstream", "--json", "-c", "--local", "--", "Dune"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertTrue(json.loads(out.getvalue())["local"])

    def test_rejects_bad_target_or_empty_title(self):
        for argv in (("continue", "tv", "Dune"), ("continue", "cast", "\x00\n")):
            out, calls = call(*argv)
            self.assertFalse(out["ok"])
            self.assertEqual(calls, [])


class RunTest(unittest.TestCase):
    def test_parses_last_json_line_and_handles_missing_binary(self):
        proc = mock.Mock(stdout='noise\n{"ok": true, "action": "x"}\n')
        with (
            mock.patch.object(helper, "nstream_bin", return_value="/bin/nstream"),
            mock.patch.object(helper.subprocess, "run", return_value=proc) as run,
        ):
            self.assertEqual(helper.run(["--status"], 5), {"ok": True, "action": "x"})
        self.assertEqual(run.call_args.args[0], ["/bin/nstream", "--json", "--status"])
        with mock.patch.object(helper, "nstream_bin", return_value=None):
            self.assertEqual(helper.run(["--status"], 5)["error"], "missing")


if __name__ == "__main__":
    unittest.main()
