from nstream import labels, ui
from nstream.config import HistoryEntry, Meta, Stream, Video
from nstream.tracks import Track, Tracks


def test_display_title_movie():
    assert labels.display_title("Dune", None) == "Dune"


def test_display_title_series_with_name():
    v = Video(season=1, episode=3, name="Ep")
    assert labels.display_title("Show", v) == "Show · S01E03 · Ep"


def test_display_title_series_without_name():
    assert labels.display_title("Show", Video(season=2, episode=10)) == "Show · S02E10"


def test_meta_label_upcoming_future_year():
    label = labels.meta_label({"type": "movie", "name": "X", "releaseInfo": "2999"})
    assert "in uscita" in label


def test_meta_label_no_hint_past_year():
    label = labels.meta_label({"type": "movie", "name": "X", "releaseInfo": "2000"})
    assert "in uscita" not in label


def test_meta_label_has_type_glyph():
    movie = labels.meta_label(Meta(type="movie", name="X", releaseInfo="2000"))
    series = labels.meta_label(Meta(type="series", name="Y", releaseInfo="2000"))
    assert ui.PORTABLE.movie in movie  # portable default in tests (no Nerd Font)
    assert ui.PORTABLE.series in series


def test_episode_label_format():
    label = labels.episode_label(Video(season=1, episode=3, name="Pilot"))
    assert "S01E03" in label and "Pilot" in label


def test_history_label_has_progress_bar():
    e = HistoryEntry(title="Dune", type="movie", position=50.0, duration=100.0)
    label = labels.history_label(e)
    assert "50%" in label
    assert "█" in label  # progress bar rendered


def test_history_label_has_type_glyph():
    movie = labels.history_label(HistoryEntry(title="Dune", type="movie", duration=0.0))
    series = labels.history_label(
        HistoryEntry(title="Show", type="series", season=1, episode=2, duration=0.0)
    )
    assert ui.PORTABLE.movie in movie
    assert ui.PORTABLE.series in series


def test_history_label_no_bar_without_duration():
    label = labels.history_label(HistoryEntry(title="Dune", type="movie", duration=0.0))
    assert "%" not in label and "█" not in label


def test_track_label_full():
    t = Track(id=1, lang="ita", codec="eac3", channels=6, title="Director")
    assert labels.track_label(t) == 'ita · eac3 · 6ch · "Director"'


def test_track_label_minimal():
    assert labels.track_label(Track(id=1, lang="", codec="", channels=0, title="")) == "und"


def test_audio_summary_auto():
    assert labels.audio_summary(None, Tracks(audio=[], subs=[])) == "automatico (lingua preferita)"


def test_sub_summary_external_wins():
    assert labels.sub_summary(3, ("/tmp/x.srt",), Tracks(audio=[], subs=[])) == (
        "OpenSubtitles (esterni)"
    )


def test_sub_summary_none():
    assert labels.sub_summary("no", (), Tracks(audio=[], subs=[])) == "nessuno"


def test_stream_label_strips_ansi_from_release_names():
    s: Stream = {"name": "Grp\x1b[2Jname", "title": "Movie\x1b]0;spoof\x07.2024"}
    label = labels.stream_label(s)
    # ESC/BEL stripped → the leftover printable chars can't be interpreted by the terminal
    assert "\x1b" not in label and "\x07" not in label
    assert "Grp" in label and "Movie" in label


def test_stream_label_shows_addon_provenance():
    s: Stream = {"name": "[RD+] 1080p", "title": "Film.mkv", "addon": "Comet"}
    assert "[Comet]" in labels.stream_label(s)
