from pathlib import Path

from anime_mpv.database import Database
from anime_mpv.manager_models import LibraryEpisode


def _episode(video: Path, *, state: str = "ready") -> LibraryEpisode:
    return LibraryEpisode(
        media_id=123,
        title="Example",
        episode=5,
        video_path=video,
        subtitle_path=None,
        embedded_subtitle_id=2,
        state=state,
        torrent_hash="abc",
    )


def test_library_rescan_does_not_change_watched_back_to_ready(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = (tmp_path / "episode.mkv").resolve()
    video.touch()

    db.upsert_episode(_episode(video))
    db.schedule_cleanup(video, 2.0)
    watched = db.episode_by_path(video)
    assert watched is not None
    assert watched.state == "watched"

    db.upsert_episode(_episode(video, state="ready"))
    rescanned = db.episode_by_path(video)
    assert rescanned is not None
    assert rescanned.state == "watched"
    assert rescanned.watched_at == watched.watched_at
    assert rescanned.delete_after == watched.delete_after


def test_reconcile_repairs_state_and_uses_current_delay(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = (tmp_path / "episode.mkv").resolve()
    video.touch()

    db.upsert_episode(_episode(video))
    db.schedule_cleanup(video, 24.0)
    watched = db.episode_by_path(video)
    assert watched is not None and watched.watched_at is not None

    with db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET state='ready' WHERE video_path=?",
            (str(video),),
        )

    assert db.reconcile_watched_cleanup(2.0) == 1
    repaired = db.episode_by_path(video)
    assert repaired is not None
    assert repaired.state == "watched"
    assert repaired.delete_after is not None
    assert abs(repaired.delete_after - (repaired.watched_at + 2 * 3600)) < 0.01


def test_anilist_progress_rollback_returns_episode_to_ready(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = (tmp_path / "episode.mkv").resolve()
    video.touch()

    db.upsert_episode(_episode(video))
    db.schedule_cleanup(video, 2.0)

    assert db.reconcile_anilist_progress(123, 4) == 1
    restored = db.episode_by_path(video)
    assert restored is not None
    assert restored.state == "ready"
    assert restored.watched_at is None
    assert restored.delete_after is None
    assert db.due_cleanup() == []


def test_anilist_progress_rollback_keeps_episodes_at_or_below_progress(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = (tmp_path / "episode.mkv").resolve()
    video.touch()

    db.upsert_episode(_episode(video))
    db.schedule_cleanup(video, 0.0)

    assert db.reconcile_anilist_progress(123, 5) == 0
    watched = db.episode_by_path(video)
    assert watched is not None
    assert watched.state == "watched"
    assert watched.watched_at is not None
    assert watched.delete_after is not None
