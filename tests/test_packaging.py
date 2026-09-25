"""Release version and shipped resources must agree across packaging formats."""

import re
from pathlib import Path

from nstream import __version__


def test_arch_versions_match_python():
    root = Path(__file__).resolve().parents[1]
    for name in ("PKGBUILD", ".SRCINFO"):
        text = (root / "packaging" / name).read_text()
        match = re.search(r"^\s*pkgver\s*=\s*(\S+)", text, re.M)
        assert match and match[1] == __version__, name
