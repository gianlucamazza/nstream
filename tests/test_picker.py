"""Unit tests for the fzf picker layer."""

from __future__ import annotations

from nstream import picker


def _stub_fzf(monkeypatch, *, returncode=0, stdout="", missing=False):
    """Stub picker.util.run_cmd: returns a CompletedProcess-like, or None when the
    fzf binary is missing. Records the argv it was called with."""
    captured = {}

    class _Proc:
        pass

    _Proc.returncode = returncode
    _Proc.stdout = stdout

    def fake(cmd, **k):
        captured["cmd"] = cmd
        return None if missing else _Proc()

    monkeypatch.setattr(picker.util, "run_cmd", fake)
    return captured


def test_fzf_passes_header_to_argv(monkeypatch):
    cap = _stub_fzf(monkeypatch, stdout="0\tlabel\n")
    picker.fzf([("a", 1), ("b", 2)], "p> ", header="avviso")
    assert "--header" in cap["cmd"]
    assert "avviso" in cap["cmd"]


def test_fzf_missing_binary_returns_none(monkeypatch, capsys):
    _stub_fzf(monkeypatch, missing=True)
    assert picker.fzf([("a", 1), ("b", 2)], "p> ") is None
    assert "fzf non trovato" in capsys.readouterr().err


def test_fzf_key_enter(monkeypatch):
    # --expect prints an empty first line for Enter, then the selection.
    cap = _stub_fzf(monkeypatch, stdout="\n1\tb\n")
    out = picker.fzf_key([("a", 10), ("b", 20)], "p> ")
    assert out == ("", 20)
    assert "--expect" in cap["cmd"] and "tab,alt-c" in cap["cmd"]


def test_fzf_key_tab(monkeypatch):
    _stub_fzf(monkeypatch, stdout="tab\n0\ta\n")
    assert picker.fzf_key([("a", 10), ("b", 20)], "p> ") == ("tab", 10)


def test_fzf_key_esc_returns_none(monkeypatch):
    _stub_fzf(monkeypatch, returncode=130, stdout="")
    assert picker.fzf_key([("a", 10), ("b", 20)], "p> ") is None


def test_fzf_key_single_item_still_launches(monkeypatch):
    # With expect set, even a one-item list opens fzf so Tab stays reachable.
    cap = _stub_fzf(monkeypatch, stdout="tab\n0\ta\n")
    assert picker.fzf_key([("a", 10)], "p> ") == ("tab", 10)
    assert "fzf" in cap["cmd"]


def test_fzf_empty_list_returns_none(monkeypatch):
    _stub_fzf(monkeypatch)
    assert picker.fzf([], "p> ") is None


def test_fzf_single_item_returned_directly(monkeypatch):
    # No header, no expect → the lone value is returned without launching fzf.
    cap = _stub_fzf(monkeypatch)
    assert picker.fzf([("only", 7)], "p> ") == 7
    assert "cmd" not in cap  # fzf never invoked
