"""Keep config.example.json and Config dataclass defaults aligned (doc drift guard)."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from nstream.config import INT_BOUNDS, Config, _ENUM_VALUES

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.json"


def _config_field_names() -> set[str]:
    return {f.name for f in fields(Config)}


def test_example_exists() -> None:
    assert EXAMPLE.is_file(), f"missing {EXAMPLE}"


def test_example_keys_match_config_fields() -> None:
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    example_keys = set(raw)
    cfg_keys = _config_field_names()
    missing = cfg_keys - example_keys
    extra = example_keys - cfg_keys
    assert not missing, f"config.example.json missing Config fields: {sorted(missing)}"
    assert not extra, f"config.example.json has unknown keys: {sorted(extra)}"


def test_example_values_match_config_defaults() -> None:
    """Defaults in the example must equal Config() so copy-paste matches code."""
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    defaults = Config()
    for name in _config_field_names():
        expected = getattr(defaults, name)
        actual = raw[name]
        assert actual == expected, (
            f"config.example.json[{name!r}] = {actual!r}, Config default = {expected!r}"
        )


def test_int_bounds_cover_bounded_fields() -> None:
    """Every INT_BOUNDS key must be a Config field (no dead validation entries)."""
    cfg_keys = _config_field_names()
    orphan = set(INT_BOUNDS) - cfg_keys
    assert not orphan, f"INT_BOUNDS keys not on Config: {sorted(orphan)}"


def test_enum_values_cover_enum_fields() -> None:
    cfg_keys = _config_field_names()
    orphan = set(_ENUM_VALUES) - cfg_keys
    assert not orphan, f"_ENUM_VALUES keys not on Config: {sorted(orphan)}"


@pytest.mark.parametrize("key", sorted(INT_BOUNDS))
def test_default_within_int_bounds(key: str) -> None:
    lo, hi = INT_BOUNDS[key]
    val = getattr(Config(), key)
    assert lo <= val <= hi, f"Config.{key}={val} outside INT_BOUNDS [{lo}, {hi}]"


@pytest.mark.parametrize("key", sorted(_ENUM_VALUES))
def test_default_in_enum(key: str) -> None:
    val = getattr(Config(), key)
    assert val in _ENUM_VALUES[key], f"Config.{key}={val!r} not in {_ENUM_VALUES[key]}"
