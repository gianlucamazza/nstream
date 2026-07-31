"""CLI parser construction (`cli_args`)."""

from nstream.cli_args import build_parser


def test_build_parser_accepts_json_cast():
    p = build_parser()
    args = p.parse_args(["--json", "--cast", "dune"])
    assert args.json and args.cast and args.query == ["dune"]


def test_build_parser_version():
    p = build_parser()
    assert "nstream" in p.prog
