"""Domain payload types (`nstream.types`)."""

from nstream.types import HistoryEntry, Meta, Stream, Subtitle, Video


def test_typeddict_keys_optional():
    s: Stream = {"url": "http://x"}
    m: Meta = {"id": "tt1", "name": "X"}
    h: HistoryEntry = {"video_id": "tt1", "position": 1.0}
    assert s["url"] and m["id"] and h["video_id"]
    _ = Subtitle(lang="ita")
    _ = Video(season=1, episode=1)
