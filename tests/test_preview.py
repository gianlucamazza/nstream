"""Unit tests for the `__preview` subcommand: metadata card formatting, poster
rendering/caching, and the best-effort guarantee (never raises, always returns 0)."""

from __future__ import annotations

from pathlib import Path

from nstream import preview, ui
from nstream.config import Config

CFG = Config(torrentio_base="tb")
CAPS_TEXT = ui.Caps(nerd_font=False, truecolor=False, image_proto=ui.ImageProto.NONE)
CAPS_IMG = ui.Caps(nerd_font=False, truecolor=True, image_proto=ui.ImageProto.SIXEL, has_chafa=True)

META = {
    "name": "Dune: Part Two",
    "type": "movie",
    "releaseInfo": "2024",
    "imdbRating": "8.3",
    "runtime": "166 min",
    "genres": ["Sci-Fi", "Adventure", "Drama"],
    "cast": ["Timothée Chalamet", "Zendaya", "Rebecca Ferguson"],
    "director": ["Denis Villeneuve"],
    "description": "Paul Atreides unites with Chani and the Fremen to wage war.",
    "poster": "http://img/dune.jpg",
}


# --- text card --------------------------------------------------------------


def test_format_meta_has_all_sections():
    out = preview._format_meta(META, CAPS_TEXT, text_width=40)
    assert "Dune: Part Two" in out
    assert "8.3" in out and "2024" in out and "166 min" in out
    assert "Sci-Fi" in out
    assert "Denis Villeneuve" in out
    assert "Timothée Chalamet" in out
    assert "Fremen" in out  # synopsis present


def test_format_meta_empty_is_safe():
    out = preview._format_meta({}, CAPS_TEXT, text_width=40)
    assert "?" in out  # placeholder title, no crash on missing fields


def test_year_parsing():
    assert preview._year({"releaseInfo": "2024-2025"}) == "2024"
    assert preview._year({"released": "1999-03-31T00:00:00.000Z"}) == "1999"
    assert preview._year({"year": 2010}) == "2010"
    assert preview._year({}) == ""


def test_find_and_format_episode():
    m = {
        "name": "Show",
        "description": "series synopsis",
        "videos": [
            {
                "season": 1,
                "episode": 1,
                "name": "Pilot",
                "overview": "first",
                "released": "2020-01-05",
            },
            {"season": 1, "episode": 2, "name": "Second"},
        ],
    }
    ep = preview._find_episode(m, 1, 1)
    assert ep and ep["name"] == "Pilot"
    out = preview._format_episode(m, ep, 1, 1, CAPS_TEXT, text_width=40)
    assert "S01E01" in out and "Pilot" in out
    assert "2020-01-05" in out
    assert "first" in out  # episode overview wins over the series synopsis


def test_format_episode_missing_falls_back_to_series_synopsis():
    m = {"name": "Show", "description": "series synopsis", "videos": []}
    out = preview._format_episode(m, None, 3, 4, CAPS_TEXT, text_width=40)
    assert "S03E04" in out
    assert "series synopsis" in out


# --- poster -----------------------------------------------------------------


def test_poster_block_none_when_no_image_proto():
    assert preview._poster_block("http://x/p.jpg", 40, 20, CAPS_TEXT, CFG) == ""


def test_poster_block_none_when_posters_disabled():
    cfg = Config(torrentio_base="tb", posters=False)
    assert preview._poster_block("http://x/p.jpg", 40, 20, CAPS_IMG, cfg) == ""


def test_poster_block_none_when_no_url():
    assert preview._poster_block("", 40, 20, CAPS_IMG, CFG) == ""


def test_poster_block_builds_chafa_argv(monkeypatch):
    monkeypatch.setattr(preview, "_cached_poster", lambda url: Path("/tmp/p.img"))
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "<sixel-bytes>"

    def fake(cmd, **k):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(preview.util, "run_cmd", fake)
    out = preview._poster_block("http://x/p.jpg", 40, 20, CAPS_IMG, CFG)
    assert out == "<sixel-bytes>"
    cmd = captured["cmd"]
    assert cmd[0] == "chafa"
    assert "--format" in cmd and "sixel" in cmd
    assert "40x12" in cmd  # cols x min(lines-8, 20) = 40x12
    assert "--colors" not in cmd  # truecolor → no 256 downgrade


def test_poster_block_symbols_adds_flags(monkeypatch):
    caps = ui.Caps(truecolor=False, image_proto=ui.ImageProto.SYMBOLS, has_chafa=True)
    monkeypatch.setattr(preview, "_cached_poster", lambda url: Path("/tmp/p.img"))
    captured = {}
    monkeypatch.setattr(preview.util, "run_cmd", lambda cmd, **k: captured.update(cmd=cmd) or _ok())
    preview._poster_block("http://x/p.jpg", 40, 20, caps, CFG)
    cmd = captured["cmd"]
    assert "symbols" in cmd and "--symbols" in cmd
    assert "--colors" in cmd and "256" in cmd


def _ok():
    class _P:
        returncode = 0
        stdout = "x"

    return _P()


def test_cached_poster_downloads_once(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(preview, "_download", lambda url: calls.append(url) or b"BYTES")
    p1 = preview._cached_poster("http://x/p.jpg")
    p2 = preview._cached_poster("http://x/p.jpg")
    assert p1 == p2 and p1 and p1.read_bytes() == b"BYTES"
    assert len(calls) == 1  # second call hits the disk cache


def test_cached_poster_download_failure_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(preview, "_download", lambda url: None)
    assert preview._cached_poster("http://x/p.jpg") is None


# --- poster cache prune -------------------------------------------------------


def _fill_posters(tmp_path, n, *, size=4):
    """Create n fake posters with increasing (old) mtimes; returns the cache dir."""
    cache_dir = tmp_path / "nstream" / "posters"
    cache_dir.mkdir(parents=True)
    import os as _os

    for i in range(n):
        p = cache_dir / f"old{i:03d}"
        p.write_bytes(b"x" * size)
        _os.utime(p, (1000 + i, 1000 + i))  # old0 is the oldest
    return cache_dir


def test_prune_on_new_poster_evicts_oldest_over_file_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(preview, "_POSTER_CACHE_MAX_FILES", 5)
    monkeypatch.setattr(preview, "_download", lambda url: b"BYTES")
    cache_dir = _fill_posters(tmp_path, 7)
    new = preview._cached_poster("http://x/new.jpg")
    assert new and new.read_bytes() == b"BYTES"  # the new poster survives the prune
    names = {p.name for p in cache_dir.iterdir()}
    assert len(names) == 5
    assert "old000" not in names and "old001" not in names and "old002" not in names
    assert "old006" in names  # newest of the old ones kept


def test_prune_enforces_byte_cap(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(preview, "_POSTER_CACHE_MAX_BYTES", 20)
    monkeypatch.setattr(preview, "_download", lambda url: b"12345")
    cache_dir = _fill_posters(tmp_path, 6, size=5)  # 30 bytes + 5 new = 35 > 20
    assert preview._cached_poster("http://x/new.jpg") is not None
    total = sum(p.stat().st_size for p in cache_dir.iterdir())
    assert total <= 20


def test_prune_noop_under_thresholds(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(preview, "_download", lambda url: b"BYTES")
    cache_dir = _fill_posters(tmp_path, 3)
    preview._cached_poster("http://x/new.jpg")
    assert len(list(cache_dir.iterdir())) == 4  # nothing evicted


def test_prune_failure_never_costs_the_poster(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(preview, "_download", lambda url: b"BYTES")

    def boom(cache_dir):
        raise RuntimeError("disk hiccup")

    monkeypatch.setattr(preview, "_prune_posters", boom)
    p = preview._cached_poster("http://x/p.jpg")
    assert p is not None and p.read_bytes() == b"BYTES"


# --- run_preview best-effort ------------------------------------------------


def test_run_preview_title_prints(monkeypatch, capsys):
    monkeypatch.setattr(preview.api, "meta_cached_disk", lambda *a: META)
    monkeypatch.setattr(preview, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(preview, "_poster_block", lambda *a: "")
    assert preview.run_preview(["title", "movie", "tt1"]) == 0
    assert "Dune: Part Two" in capsys.readouterr().out


def test_run_preview_never_raises(monkeypatch):
    def boom(*a):
        raise RuntimeError("network down")

    monkeypatch.setattr(preview.api, "meta_cached_disk", boom)
    monkeypatch.setattr(preview, "_load_cfg", lambda: CFG)
    assert preview.run_preview(["title", "movie", "tt1"]) == 0


def test_run_preview_bad_args_returns_zero():
    assert preview.run_preview([]) == 0
    assert preview.run_preview(["episode", "ttX", "notint", "1"]) == 0


# --- run_layout (resize transform) -------------------------------------------


def _run_layout(monkeypatch, capsys, cols, lines):
    monkeypatch.setenv("FZF_COLUMNS", str(cols))
    monkeypatch.setenv("FZF_LINES", str(lines))
    monkeypatch.setattr(preview, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(preview.ui, "detect_caps", lambda cfg: CAPS_IMG)
    assert preview.run_layout() == 0
    return capsys.readouterr().out.strip()


def test_run_layout_breakpoints(monkeypatch, capsys):
    assert _run_layout(monkeypatch, capsys, 120, 40) == (
        "change-preview-window(right:50%:wrap)+refresh-preview"
    )
    assert _run_layout(monkeypatch, capsys, 60, 30) == (
        "change-preview-window(down:45%:wrap)+refresh-preview"
    )
    assert _run_layout(monkeypatch, capsys, 40, 10) == (
        "change-preview-window(hidden)+refresh-preview"
    )


def test_run_layout_never_raises(monkeypatch, capsys):
    def boom(cfg):
        raise RuntimeError("caps detection down")

    monkeypatch.setenv("FZF_COLUMNS", "100")
    monkeypatch.setenv("FZF_LINES", "30")
    monkeypatch.setattr(preview, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(preview.ui, "detect_caps", boom)
    assert preview.run_layout() == 0
    assert capsys.readouterr().out == ""  # empty output = fzf no-op, pane untouched


def test_poster_download_is_size_capped(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"x" * (n if n > 0 else 10**9)

    monkeypatch.setattr(preview.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert preview._download("http://img.example/p.jpg") is None
