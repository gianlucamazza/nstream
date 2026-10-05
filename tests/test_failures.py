"""One rendering of domain failures for the TUI and --json (ADR 0037)."""

from __future__ import annotations

import pytest

from nstream import api, availability, cast_flow, failures, stream_select
from nstream.caster import CastUnavailable


def test_payload_appends_cli_hint_only_to_json():
    f = failures.describe(cast_flow.CastRemuxInfeasible("spazio disco insufficiente"))
    assert f.code == "remux_infeasible" and "--quality" not in f.message
    p = f.payload()
    assert p["ok"] is False and p["error"] == "remux_infeasible"
    assert "--quality 1080" in p["message"] and p["reason"] == "spazio disco insufficiente"


def test_quality_lists_tiers_from_exception_or_context():
    f = failures.describe(stream_select.QualityUnavailable(1080, []), title="Dune",
                          available_resolutions=[720])  # fmt: skip
    assert f.fields == {"available_resolutions": [720]} and "720p" in f.message


def test_audio_lang_real_tracks_wording_shared_by_both_frontends():
    e = stream_select.AudioLangUnavailable("ita", ("eng",), real_tracks=True)
    f = failures.describe(e, title="Dune")
    assert f.code == "audio_lang_unavailable" and "tracce reali" in f.message
    assert f.fields == {"available_audio": ["eng"]}


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (cast_flow.CastStreamUnresolved(), "no_playable_stream"),
        (cast_flow.CastVideoUnsupported("mpeg4"), "video_codec_unsupported"),
        (CastUnavailable("nessun Chromecast"), "device_not_found"),
    ],
)
def test_codes(exc, code):
    assert failures.describe(exc).code == code


def test_truncated_fields():
    v = availability.DurationVerdict(False, 30.0, 3600.0, "durata 0:30 contro ~60 min attesi")
    f = failures.describe(stream_select.ContentTooShort(v, count=2), title="X")
    assert f.code == "sources_truncated" and f.fields["truncated_sources"] == 2


def test_unknown_exception_is_a_programming_error():
    with pytest.raises(TypeError):
        failures.describe(ValueError("x"))


def test_id_untranslated_code_and_catalog_id():
    f = failures.describe(api.IdUntranslated("tmdb:1"), title="X")
    assert f.code == "id_untranslated"
    assert f.fields == {"catalog_id": "tmdb:1"}
    assert "tmdb:1" in f.message and "IMDb" in f.message
    assert f.payload()["error"] == "id_untranslated"
