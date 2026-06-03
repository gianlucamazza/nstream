"""Unit tests for settings: pure helpers plus the interactive editor flows
(fzf/getpass/_ask are stubbed so no tty is needed)."""

from __future__ import annotations

import pytest

from nstream import settings
from nstream.addons import Addon
from nstream.config import Config


def test_token_status():
    assert (
        settings._token_status(Config(torrentio_base="sort=x|realdebrid=ABCD"))
        == "✓ RealDebrid (••••)"
    )
    assert settings._token_status(Config(torrentio_base="sort=x|realdebrid=")) == "✗ assente"
    assert settings._token_status(Config(torrentio_base="sort=x")) == "✗ assente"


def test_token_status_other_providers():
    assert (
        settings._token_status(Config(torrentio_base="sort=x|alldebrid=K")) == "✓ AllDebrid (••••)"
    )
    assert settings._token_status(Config(torrentio_base="torbox=K")) == "✓ TorBox (••••)"
    assert settings._token_status(Config(torrentio_base="sort=x|putio=K")) == "✓ Put.io (••••)"


def test_with_token_replaces_only_token():
    base = "sort=qualitysize|realdebrid=OLD"
    out = settings._with_token(base, "NEW")
    assert out == "sort=qualitysize|realdebrid=NEW"
    assert "OLD" not in out


def test_with_token_from_empty():
    assert settings._with_token("", "T") == "sort=qualitysize|realdebrid=T"


def test_with_token_switches_provider():
    # Switching debrid drops the old provider segment, keeps sort, leaves one provider.
    out = settings._with_token("sort=qualitysize|realdebrid=OLD", "K", "alldebrid")
    assert out == "sort=qualitysize|alldebrid=K"
    assert "realdebrid" not in out


def test_items_cover_all_settings():
    cfg = Config(torrentio_base="sort=x|realdebrid=T")
    keys = [it[0] for it in settings._items(cfg)]
    assert keys == [
        "audio_langs",
        "subtitle_langs",
        "auto_play",
        "prefer_cast",
        "cast_device",
        "autoplay",
        "autoplay_lead",
        "hwdec",
        "history_enabled",
        "mpv_quiet",
        "hw_filter",
        "max_resolution",
        "allow_software",
        "allow_dv5",
        "lang_filter",
        "exclude_camrip",
        "min_seeders",
        "dedup",
        "max_streams",
        "nerd_font",
        "posters",
        "image_mode",
        "torrentio_base",
        "__addons__",
    ]


def test_ask_returns_empty_on_eof(monkeypatch):
    def boom(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert settings._ask("x> ") == ""


def test_mpv_quiet_in_items():
    keys = [it[0] for it in settings._items(Config(torrentio_base="sort=x|realdebrid=T"))]
    assert "mpv_quiet" in keys


def test_items_render_current_values():
    cfg = Config(torrentio_base="sort=x|realdebrid=T", audio_langs=["jpn"], autoplay=False)
    by_key = {it[0]: it for it in settings._items(cfg)}
    assert by_key["audio_langs"][3] == "日本語"  # rendered via the language registry name
    assert by_key["autoplay"][3] == "off"
    assert by_key["torrentio_base"][3] == "✓ RealDebrid (••••)"


# --- device discovery (Fase 1s) --------------------------------------------


def test_scan_devices_parses_catt_scan(monkeypatch):
    class _P:
        returncode = 0
        stdout = (
            "Scanning Chromecasts...\n"
            "192.0.2.10 - 43PUS9235/12 - Philips TPM191E\n"
            "192.168.1.50 - Soggiorno - Google Nest\n"
            "192.0.2.10 - 43PUS9235/12 - Philips TPM191E\n"  # dup
        )

    monkeypatch.setattr(settings.util, "run_cmd", lambda *a, **k: _P())
    assert settings.scan_devices() == [
        ("43PUS9235/12", "192.0.2.10"),
        ("Soggiorno", "192.168.1.50"),
    ]


def test_scan_devices_empty_on_failure(monkeypatch):
    # run_cmd returns None when catt is missing or the scan fails.
    monkeypatch.setattr(settings.util, "run_cmd", lambda *a, **k: None)
    assert settings.scan_devices() == []


def test_cast_device_item_present():
    keys = [it[0] for it in settings._items(Config(torrentio_base="sort=x|realdebrid=T"))]
    assert "cast_device" in keys


# --- _edit: per-kind editors ----------------------------------------------

RD = Config(torrentio_base="sort=qualitysize|realdebrid=TOK")


def _capture_save(monkeypatch):
    """Capture config.save payloads instead of touching disk."""
    saved = []
    monkeypatch.setattr(settings.config, "save", lambda upd: saved.append(upd))
    return saved


def test_edit_bool_toggles(monkeypatch):
    saved = _capture_save(monkeypatch)
    settings._edit(Config(torrentio_base="tb", mpv_quiet=True), "mpv_quiet", "bool", "Q")
    assert saved == [{"mpv_quiet": False}]


def test_edit_int_clamps_to_ceiling(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_ask", lambda *a: "999")
    settings._edit(RD, "min_seeders", "int", "Seeders")
    assert saved == [{"min_seeders": 100}]  # clamped to INT_BOUNDS ceiling


def test_edit_int_invalid_no_save(monkeypatch, capsys):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_ask", lambda *a: "abc")
    settings._edit(RD, "autoplay_lead", "int", "Lead")
    assert saved == []
    assert "valore non valido" in capsys.readouterr().err


def test_edit_int_empty_no_save(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_ask", lambda *a: "")
    settings._edit(RD, "autoplay_lead", "int", "Lead")
    assert saved == []


def test_edit_langs_saves_multiselect(monkeypatch):
    saved = _capture_save(monkeypatch)
    # fzf_multi returns the marked codes in display order; they're saved verbatim.
    monkeypatch.setattr(settings.picker, "fzf_multi", lambda items, prompt, **k: ["eng", "ita"])
    settings._edit(RD, "audio_langs", "langs", "Audio")
    assert saved == [{"audio_langs": ["eng", "ita"]}]


def test_edit_langs_empty_is_noop(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings.picker, "fzf_multi", lambda items, prompt, **k: None)  # ESC
    settings._edit(RD, "audio_langs", "langs", "Audio")
    assert saved == []  # nothing marked → preferences untouched


def test_pick_languages_lists_current_first(monkeypatch):
    captured = {}

    def fake_multi(items, prompt, **k):
        captured["codes"] = [code for _, code in items]
        return None

    monkeypatch.setattr(settings.picker, "fzf_multi", fake_multi)
    settings._pick_languages(["eng"], "Audio")
    # the already-selected code leads, the rest follow in registry order
    assert captured["codes"][0] == "eng"
    assert "jpn" in captured["codes"]  # extended registry is offered
    assert "multi" not in captured["codes"]  # the multi attribute isn't selectable


def test_edit_enum_hwdec_sets_value(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(
        settings, "_fzf_select", lambda *a, **k: settings.HWDEC_CHOICES.index("vaapi")
    )
    settings._edit(RD, "hwdec", "enum", "HW")
    assert saved == [{"hwdec": "vaapi"}]


def test_edit_enum_hwdec_disable(monkeypatch):
    saved = _capture_save(monkeypatch)
    no_idx = next(i for i, c in enumerate(settings.HWDEC_CHOICES) if c.startswith("no"))
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: no_idx)
    settings._edit(RD, "hwdec", "enum", "HW")
    assert saved == [{"hwdec": ""}]


def test_edit_maxres(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: 1)
    settings._edit(RD, "max_resolution", "maxres", "Res")
    assert saved == [{"max_resolution": settings.MAXRES_CHOICES[1][1]}]


def test_edit_castdev_auto(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "scan_devices", lambda: [("TV", "1.2.3.4")])
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: 0)  # (auto)
    settings._edit(RD, "cast_device", "castdev", "Dev")
    assert saved == [{"cast_device": ""}]


def test_edit_castdev_named(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "scan_devices", lambda: [("TV", "1.2.3.4")])
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: 1)  # "TV"
    settings._edit(RD, "cast_device", "castdev", "Dev")
    assert saved == [{"cast_device": "TV"}]


def test_edit_castdev_esc_no_save(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "scan_devices", lambda: [])
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: None)
    settings._edit(RD, "cast_device", "castdev", "Dev")
    assert saved == []


def test_edit_token_sets_provider(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: 1)  # AllDebrid
    monkeypatch.setattr(settings.getpass, "getpass", lambda *a: "NEWTOK")
    settings._edit(
        Config(torrentio_base="sort=qualitysize|realdebrid=OLD"),
        "torrentio_base",
        "token",
        "Debrid",
    )
    assert saved == [{"torrentio_base": "sort=qualitysize|alldebrid=NEWTOK"}]


def test_edit_token_empty_no_save(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: 0)
    monkeypatch.setattr(settings.getpass, "getpass", lambda *a: "  ")
    settings._edit(RD, "torrentio_base", "token", "Debrid")
    assert saved == []


# --- run_settings loop -----------------------------------------------------


def test_run_settings_edits_then_exits(monkeypatch):
    idxs = iter([0, None])  # pick the first row, then ESC
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: next(idxs))
    monkeypatch.setattr(settings.config, "load", lambda: RD)
    edited = []
    monkeypatch.setattr(settings, "_edit", lambda cfg, key, kind, label: edited.append(key))
    settings.run_settings(RD)
    assert edited == [settings._items(RD)[0][0]]


def test_run_settings_esc_immediately(monkeypatch):
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(settings, "_edit", lambda *a: called.append(1))
    settings.run_settings(RD)
    assert called == []


# --- _add_addon ------------------------------------------------------------


def test_add_addon_empty_url_no_save(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_ask", lambda *a: "")
    settings._add_addon(RD)
    assert saved == []


def test_add_addon_rejects_non_manifest(monkeypatch, capsys):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_ask", lambda *a: "http://x/bad")
    settings._add_addon(RD)
    assert saved == []
    assert "manifest.json" in capsys.readouterr().err


def test_add_addon_rejects_duplicate(monkeypatch, capsys):
    saved = _capture_save(monkeypatch)
    cfg = Config(torrentio_base="tb", addons=["http://x/manifest.json"])
    monkeypatch.setattr(settings, "_ask", lambda *a: "http://x/manifest.json")
    settings._add_addon(cfg)
    assert saved == []
    assert "già presente" in capsys.readouterr().err


def test_add_addon_unreachable(monkeypatch, capsys):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_ask", lambda *a: "http://x/manifest.json")
    monkeypatch.setattr(settings.addons, "load_addon", lambda url, **k: None)
    settings._add_addon(RD)
    assert saved == []
    assert "non raggiungibile" in capsys.readouterr().err


def test_add_addon_success(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_ask", lambda *a: "http://x/manifest.json")
    monkeypatch.setattr(
        settings.addons,
        "load_addon",
        lambda url, **k: Addon(base="http://x", name="X", resources={"stream": {}}),
    )
    settings._add_addon(Config(torrentio_base="tb", addons=[]))
    assert saved == [{"addons": ["http://x/manifest.json"]}]


# --- _addons_menu ----------------------------------------------------------


def test_addons_menu_add_row_calls_add(monkeypatch):
    cfg = Config(torrentio_base="tb")
    monkeypatch.setattr(settings.addons, "effective_addons", lambda c: [])
    monkeypatch.setattr(settings.config, "load", lambda: cfg)
    idxs = iter([0, None])  # the only row is "➕ Aggiungi…" (len-1), then ESC
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: next(idxs))
    added = []
    monkeypatch.setattr(settings, "_add_addon", lambda c: added.append(1))
    settings._addons_menu(cfg)
    assert added == [1]


def test_addons_menu_removes_extra(monkeypatch):
    saved = _capture_save(monkeypatch)
    extra = Addon(
        base="http://x", name="X", resources={"stream": {}}, manifest_url="http://x/manifest.json"
    )
    cfg = Config(torrentio_base="tb", addons=["http://x/manifest.json"])
    monkeypatch.setattr(settings.addons, "effective_addons", lambda c: [extra])
    monkeypatch.setattr(settings.config, "load", lambda: cfg)
    idxs = iter([0, None])  # pick the extra, then ESC
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: next(idxs))
    monkeypatch.setattr(settings, "_ask", lambda *a: "y")  # confirm removal
    settings._addons_menu(cfg)
    assert saved == [{"addons": []}]


def test_addons_menu_builtin_not_removable(monkeypatch, capsys):
    saved = _capture_save(monkeypatch)
    builtin = Addon(base="http://c", name="Cinemeta", resources={"catalog": {}}, builtin=True)
    cfg = Config(torrentio_base="tb")
    monkeypatch.setattr(settings.addons, "effective_addons", lambda c: [builtin])
    idxs = iter([0, None])
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: next(idxs))
    settings._addons_menu(cfg)
    assert saved == []
    assert "built-in" in capsys.readouterr().err


# --- onboard ---------------------------------------------------------------


def test_onboard_writes_config(monkeypatch):
    saved = _capture_save(monkeypatch)
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: 0)  # RealDebrid
    monkeypatch.setattr(settings.getpass, "getpass", lambda *a: "TOK")
    settings.onboard()
    assert saved == [{"torrentio_base": "sort=qualitysize|realdebrid=TOK"}]


def test_onboard_no_provider_raises(monkeypatch):
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: None)
    with pytest.raises(settings.config.ConfigError):
        settings.onboard()


def test_onboard_no_token_raises(monkeypatch):
    monkeypatch.setattr(settings, "_fzf_select", lambda *a, **k: 0)
    monkeypatch.setattr(settings.getpass, "getpass", lambda *a: "")
    with pytest.raises(settings.config.ConfigError):
        settings.onboard()
