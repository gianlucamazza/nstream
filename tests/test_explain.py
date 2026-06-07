"""Unit tests for the --explain diagnostic renderer."""

from __future__ import annotations

from nstream import explain, tracks
from nstream.config import Config, Stream

CFG = Config(torrentio_base="tb", audio_langs=["ita", "eng"])

S_4K_ITA: Stream = {
    "name": "[RD+] Torrentio\n4k",
    "title": "Movie.2024.2160p.BluRay.HEVC.ITA.ENG-GRP\n👤 30 💾 20.0 GB ⚙️ x",
    "url": "http://x/a",
}
S_1080_UNTAGGED: Stream = {
    "name": "Torrentio\n1080p",
    "title": "Movie.2024.1080p.WEBRip.x264-GRP\n👤 10 💾 6.0 GB ⚙️ x",
    "url": "http://x/b",
}
S_8K: Stream = {
    "name": "Torrentio\n4320p",
    "title": "Movie.2024.4320p.AI.Upscale-GRP\n👤 2 💾 80.0 GB ⚙️ x",
    "url": "http://x/c",
}


# --- explain_streams --------------------------------------------------------


def test_explain_streams_header_and_pick():
    out = explain.explain_streams(CFG, [S_4K_ITA, S_1080_UNTAGGED, S_8K], cast=False)
    assert "profilo LOCALE" in out
    assert "Caps:" in out and "Filtri:" in out
    assert "PLAYABLE" in out
    assert "✓PICK" in out
    # The 4K cached preferred-language BluRay is the auto-pick; the 8K is excluded.
    pick_line = next(line for line in out.splitlines() if "✓PICK" in line)
    assert "2160p" in pick_line
    assert "ESCLUSI" in out and "8K" in out


def test_explain_streams_cast_profile_label():
    out = explain.explain_streams(CFG, [S_4K_ITA], cast=True)
    assert "profilo CAST" in out


def test_explain_streams_empty():
    assert "nessuno stream" in explain.explain_streams(CFG, [], cast=False)


def test_auto_pick_returns_best():
    pick = explain.auto_pick(CFG, [S_1080_UNTAGGED, S_4K_ITA], cast=False)
    assert pick is not None and pick.stream is S_4K_ITA


# --- explain_audio ----------------------------------------------------------


def test_explain_audio_no_pick():
    out = explain.explain_audio(CFG, None)
    assert "nessuno stream giocabile" in out


def test_explain_audio_lists_tracks_and_choice(monkeypatch):
    pick = explain.auto_pick(CFG, [S_4K_ITA], cast=False)
    fake = tracks.Tracks(
        audio=[
            tracks.Track(id=1, lang="eng", codec="eac3", channels=6),
            tracks.Track(id=2, lang="ita", codec="ac3", channels=6, title="Italiano"),
        ]
    )
    monkeypatch.setattr(explain.tracks, "probe_tracks", lambda url: fake)
    out = explain.explain_audio(CFG, pick)
    assert "--alang=ita,eng" in out
    assert "aid=1 eng" in out and "aid=2 ita" in out
    # ita is preferred first → mpv picks aid=2.
    choice_line = next(line for line in out.splitlines() if "→ mpv sceglie" in line)
    assert "aid=2" in choice_line


def test_explain_audio_no_preferred_track_warns(monkeypatch):
    pick = explain.auto_pick(CFG, [S_4K_ITA], cast=False)
    fake = tracks.Tracks(audio=[tracks.Track(id=1, lang="fra", codec="ac3")])
    monkeypatch.setattr(explain.tracks, "probe_tracks", lambda url: fake)
    out = explain.explain_audio(CFG, pick)
    assert "Nessuna traccia nelle lingue preferite" in out


def test_explain_audio_guard_note_when_primary_missing(monkeypatch):
    # The pick carries only fallback audio (eng): explain must surface the language guard
    # (reselect toward a primary-tagged source, else safety subtitles) so --explain matches
    # what playback would actually do.
    pick = explain.auto_pick(CFG, [S_4K_ITA], cast=False)
    fake = tracks.Tracks(audio=[tracks.Track(id=1, lang="eng", codec="eac3", channels=6)])
    monkeypatch.setattr(explain.tracks, "probe_tracks", lambda url: fake)
    out = explain.explain_audio(CFG, pick)
    assert "Guardia lingua" in out and "sorgente taggata ita" in out


def test_explain_audio_no_ffprobe(monkeypatch):
    pick = explain.auto_pick(CFG, [S_4K_ITA], cast=False)
    monkeypatch.setattr(explain.tracks, "probe_tracks", lambda url: tracks.Tracks())
    out = explain.explain_audio(CFG, pick)
    assert "ffprobe non disponibile" in out


def test_mpv_audio_choice_order():
    audio = [
        tracks.Track(id=1, lang="eng"),
        tracks.Track(id=2, lang="ita"),
    ]
    # ita preferred first → aid=2 even though eng appears earlier in the file.
    ita_first = explain._mpv_audio_choice(audio, ["ita", "eng"])
    eng_first = explain._mpv_audio_choice(audio, ["eng", "ita"])
    assert ita_first is not None and ita_first.id == 2
    assert eng_first is not None and eng_first.id == 1
    assert explain._mpv_audio_choice(audio, ["jpn"]) is None


def test_mpv_audio_choice_matches_two_letter_tags():
    # Container tags are often ISO-639-1 (it/en) while prefs are 3-letter (ita/eng).
    audio = [tracks.Track(id=1, lang="en"), tracks.Track(id=2, lang="it")]
    chosen = explain._mpv_audio_choice(audio, ["ita", "eng"])
    assert chosen is not None and chosen.id == 2  # "it" matches "ita"
