from pathlib import Path

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode


def _version(db: Database) -> int:
    return int(db.get_state("ui_state_version", "0") or 0)


def test_visible_database_changes_bump_ui_state_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    before = _version(db)
    db.upsert_anime(LibraryAnime(media_id=1, title="A", status="CURRENT", progress=0, episodes=12))
    after_anime = _version(db)
    assert after_anime > before

    video = tmp_path / "A - 01.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(LibraryEpisode(media_id=1, title="A", episode=1, video_path=video, state="local"))
    after_episode = _version(db)
    assert after_episode > after_anime

    db.set_subtitle_ready(video, None, origin="embedded")
    assert _version(db) > after_episode


def test_playback_heartbeat_does_not_bump_ui_state_version(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=2, title="Movie", status="CURRENT", progress=0, episodes=1))
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(LibraryEpisode(media_id=2, title="Movie", episode=1, video_path=video, state="ready"))
    before = _version(db)

    db.record_playback(video, 120.0, 6000.0, active_seconds=1.0)

    assert _version(db) == before
