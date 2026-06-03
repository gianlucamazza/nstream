"""Unit tests for the fzf picker layer: the colour theme applied to every menu, the
hidden-field row scheme, and the optional per-row preview wiring."""

from __future__ import annotations

import pytest

from nstream import picker, ui


@pytest.fixture(autouse=True)
def _pin_caps(monkeypatch):
    """Pin deterministic caps so theme/preview flags don't depend on the host terminal
    or touch the on-disk caps cache."""
    monkeypatch.setattr(
        ui,
        "_active",
        ui.Caps(nerd_font=False, truecolor=True, image_proto=ui.ImageProto.SIXEL, has_chafa=True),
    )


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


# --- theme (applied to every menu) -----------------------------------------


def test_every_menu_carries_theme(monkeypatch):
    cap = _stub_fzf(monkeypatch, stdout="0\tlabel\n")
    picker.fzf([("a", 1), ("b", 2)], "p> ")
    assert "--ansi" in cap["cmd"]
    assert "--color" in cap["cmd"]
    assert "--pointer" in cap["cmd"] and "▶" in cap["cmd"]  # portable glyph
    assert "--with-nth" in cap["cmd"] and "2.." in cap["cmd"]  # no preview → 2 fields


def test_fzf_passes_header_to_argv(monkeypatch):
    cap = _stub_fzf(monkeypatch, stdout="0\tlabel\n")
    picker.fzf([("a", 1), ("b", 2)], "p> ", header="avviso")
    assert "--header" in cap["cmd"]
    assert "avviso" in cap["cmd"]


def test_fzf_missing_binary_returns_none(monkeypatch, capsys):
    _stub_fzf(monkeypatch, missing=True)
    assert picker.fzf([("a", 1), ("b", 2)], "p> ") is None
    assert "fzf non trovato" in capsys.readouterr().err


# --- preview wiring ---------------------------------------------------------


def test_preview_adds_three_field_rows_and_command(monkeypatch):
    cap = _stub_fzf(monkeypatch, stdout="1\ttitle movie tt2\tb\n")
    out = picker.fzf([("a", 1), ("b", 2)], "p> ", preview=lambda v: f"title movie tt{v}")
    assert out == 2  # index field still parsed correctly
    cmd = cap["cmd"]
    assert "3.." in cmd  # visible label moved to field 3
    assert "--preview" in cmd
    assert any("__preview {2}" in c for c in cmd)
    assert "--preview-window" in cmd


def test_preview_none_emits_blank_field(monkeypatch):
    cap = _stub_fzf(monkeypatch, stdout="0\t\ta\n")
    picker.fzf([("a", 1)], "p> ", preview=lambda v: None)
    # the lone item still launches fzf (preview reachable) and emits an empty field 2
    assert "fzf" in cap["cmd"]
    # input lines are passed via run_cmd input=, not argv; assert the row scheme via with-nth
    assert "3.." in cap["cmd"]


# --- fzf_key (Enter/Tab/ESC) ------------------------------------------------


def test_fzf_key_enter(monkeypatch):
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


def test_fzf_key_with_preview_three_fields(monkeypatch):
    cap = _stub_fzf(monkeypatch, stdout="tab\n0\ttitle movie tt10\ta\n")
    out = picker.fzf_key([("a", 10)], "p> ", preview=lambda v: f"title movie tt{v}")
    assert out == ("tab", 10)
    assert "3.." in cap["cmd"] and "--expect" in cap["cmd"]


# --- single-item shortcut ---------------------------------------------------


# --- multi-select -----------------------------------------------------------


def test_fzf_multi_returns_marked_in_order(monkeypatch):
    cap = _stub_fzf(monkeypatch, stdout="1\teng\n0\tita\n")
    out = picker.fzf_multi([("ita", "ita"), ("eng", "eng"), ("fra", "fra")], "lang> ")
    assert out == ["eng", "ita"]  # marked rows, in fzf output order
    assert "--multi" in cap["cmd"]


def test_fzf_multi_esc_returns_none(monkeypatch):
    _stub_fzf(monkeypatch, returncode=130, stdout="")
    assert picker.fzf_multi([("ita", "ita"), ("eng", "eng")], "lang> ") is None


def test_fzf_multi_empty_output_returns_none(monkeypatch):
    _stub_fzf(monkeypatch, returncode=0, stdout="\n")
    assert picker.fzf_multi([("ita", "ita")], "lang> ") is None


def test_fzf_multi_missing_binary_returns_none(monkeypatch, capsys):
    _stub_fzf(monkeypatch, missing=True)
    assert picker.fzf_multi([("ita", "ita")], "lang> ") is None
    assert "fzf non trovato" in capsys.readouterr().err


def test_fzf_empty_list_returns_none(monkeypatch):
    _stub_fzf(monkeypatch)
    assert picker.fzf([], "p> ") is None


def test_fzf_single_item_returned_directly(monkeypatch):
    # No header, no expect, no preview → the lone value is returned without launching fzf.
    cap = _stub_fzf(monkeypatch)
    assert picker.fzf([("only", 7)], "p> ") == 7
    assert "cmd" not in cap  # fzf never invoked
