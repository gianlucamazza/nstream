"""Unit tests for stream-source presets and playable-stream filter."""

from __future__ import annotations

from nstream import sources


def test_normalization_rejects_local_file_and_malformed_fields():
    assert sources.normalize_stream({"url": "file:///etc/passwd"}) is None
    assert sources.normalize_stream({"url": "--script=malicious"}) is None
    assert sources.normalize_stream({"url": ["https://example.test"]}) is None
    row = sources.normalize_stream(
        {
            "url": "https://example.test/video",
            "fileIdx": -1,
            "behaviorHints": {"filename": [], "videoSize": {}},
            "title": 17,
        }
    )
    assert row is not None
    assert "fileIdx" not in row and "title" not in row
    assert row["behaviorHints"] == {}


def test_presets_catalog_nonempty_unique_ids():
    assert sources.STREAM_PRESETS
    ids = [p.id for p in sources.STREAM_PRESETS]
    assert len(ids) == len(set(ids))
    for p in sources.STREAM_PRESETS:
        assert p.configure_url.startswith("https://")
        assert "stream" in p.resources


def test_preset_lookup():
    preset = sources.preset("comet")
    assert preset is not None and preset.name == "Comet"
    assert sources.preset("nope") is None
    assert sources.preset_by_name("MediaFusion") is not None
    assert sources.preset_by_name("mediafusion") is not None
    assert sources.preset_by_name("unknown") is None


def test_is_playable_stream():
    assert sources.is_playable_stream({"url": "http://x"}) is True
    assert sources.is_playable_stream({"infoHash": "abc"}) is True
    assert sources.is_playable_stream({"url": "http://x", "infoHash": "abc"}) is True
    # Stremio shapes nstream does not play in-process
    assert sources.is_playable_stream({"ytId": "dQw4w9WgXcQ"}) is False
    assert sources.is_playable_stream({"externalUrl": "https://netflix.com/x"}) is False
    assert sources.is_playable_stream({}) is False
    assert sources.is_playable_stream(None) is False
    assert sources.is_playable_stream("nope") is False


def test_no_stream_source_message():
    msg = sources.no_stream_source_message()
    assert "fonte stream" in msg
    assert "Torrentio" in msg
