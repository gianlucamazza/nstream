"""CLI parser construction (`cli_args`)."""

from nstream.cli_args import build_parser, headless_only_misuse


def test_build_parser_accepts_json_cast():
    p = build_parser()
    args = p.parse_args(["--json", "--cast", "dune"])
    assert args.json and args.cast and args.query == ["dune"]


def test_build_parser_version():
    p = build_parser()
    assert "nstream" in p.prog


def test_headless_only_misuse_without_json():
    p = build_parser()
    args = p.parse_args(["--stop"])
    msg = headless_only_misuse(args)
    assert msg is not None and "--stop" in msg and "--json" in msg


def test_headless_only_misuse_ok_with_json():
    p = build_parser()
    args = p.parse_args(["--json", "--stop"])
    assert headless_only_misuse(args) is None


def test_headless_only_misuse_year_device_probe():
    p = build_parser()
    args = p.parse_args(["--year", "1999", "--device", "TV", "--probe", "x"])
    msg = headless_only_misuse(args)
    assert msg is not None
    assert "--year" in msg and "--device" in msg and "--probe" in msg


def test_audio_lang_is_not_headless_only():
    """--audio-lang holds on TUI too; must not be rejected without --json."""
    p = build_parser()
    args = p.parse_args(["--audio-lang", "eng", "dune"])
    assert headless_only_misuse(args) is None
    assert args.audio_lang == "eng"


def test_volume_help_names_cast_percent_and_osd_mismatch():
    """`--volume` is Cast 0–100%, not TV OSD; Phase 0 MASTER / step=null / 14→13."""
    help_txt = build_parser().format_help()
    assert "volume Cast 0-100%" in help_txt
    assert "SET_VOLUME 0-1" in help_txt
    assert "OSD" in help_txt
    assert "volume_control_type=master" in help_txt
    assert "volume_step_interval=null" in help_txt
    assert "14→13" in help_txt
