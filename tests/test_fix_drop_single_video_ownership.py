"""A single video dropped on the window stays the user's file: never deleted, never forgotten."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0, reason="needs ffmpeg"
)


def _tiny_mkv(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=2",
            "-c:v",
            "libx264",
            "-y",
            str(path),
        ],
        check=True,
    )


def _api(tmp_path: Path, *, progress: int):
    api = make_api(tmp_path)
    api.manager.config.anilist.enabled = False
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=1, title="Frieren", status="CURRENT", episodes=28, format="TV", progress=progress
        )
    )
    return api


def _downloads_video(tmp_path: Path, *, sidecar: bool = True) -> tuple[Path, Path]:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    video = downloads / "[Group] Frieren - 05 (1080p).mkv"
    _tiny_mkv(video)
    subtitle = downloads / "[Group] Frieren - 05 (1080p).ja.srt"
    if sidecar:
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    return video, subtitle


def _downloaded_at(api, video: Path):
    with api.manager.db.connect() as conn:
        row = conn.execute(
            "SELECT downloaded_at FROM episodes WHERE video_path=?", (str(video.resolve()),)
        ).fetchone()
    assert row is not None
    return row["downloaded_at"]


def _later(monkeypatch, hours: float) -> None:
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + hours * 3600)


def test_dropped_video_survives_cleanup_after_watching(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path, progress=4)
    video, subtitle = _downloads_video(tmp_path)
    try:
        result = api.import_dropped_paths([str(video)])
        assert result["ok"], result
        assert _downloaded_at(api, video) is None
        api.manager._scan_library_uncached()
        # cli.py after an mpv watch: cleanup with the configured delay (24 h).
        Database(api.manager.config.library.database_path).schedule_cleanup(
            video.resolve(),
            api.manager.config.agent.delete_after_watched_hours,
            list_status="CURRENT",
            media_id=1,
            episode=5,
        )
        _later(monkeypatch, 25)
        api.manager.cleanup()
        assert video.exists() and subtitle.exists()
    finally:
        api.close()


def test_dropped_video_covered_by_anilist_progress_is_not_deleted(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path, progress=10)  # never played; AniList already counts episode 5
    video, subtitle = _downloads_video(tmp_path)
    try:
        api.import_dropped_paths([str(video)])
        api.manager._scan_library_uncached()
        api.manager.cleanup()
        _later(monkeypatch, 25)
        api.manager._scan_library_uncached()
        api.manager.cleanup()
        assert video.exists() and subtitle.exists()
        row = api.manager.db.episode_by_path(video.resolve())
        assert row is not None and row.delete_after is None
    finally:
        api.close()


def test_redropping_a_watched_folder_file_keeps_it_unmanaged(tmp_path: Path) -> None:
    api = _api(tmp_path, progress=4)
    external = tmp_path / "External"
    external.mkdir()
    api.manager.config.paths.download_dirs = [external]
    video = external / "[Group] Frieren - 05 (1080p).mkv"
    _tiny_mkv(video)
    (external / "[Group] Frieren - 05 (1080p).ja.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8"
    )
    try:
        api.manager._scan_library_uncached()
        assert _downloaded_at(api, video) is None
        api.import_dropped_paths([str(video)])
        assert _downloaded_at(api, video) is None
        # Inside a watched folder: found by the scan, not remembered on its own.
        assert api.manager.dropped_media_files() == ()
    finally:
        api.close()


def test_redrop_keeps_downloaded_at_of_pudges_own_download(tmp_path: Path) -> None:
    api = _api(tmp_path, progress=0)
    root = api.manager.config.library.root_dir
    folder = root / "Frieren"
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "[Group] Frieren - 05 (1080p).mkv"
    _tiny_mkv(video)
    stamp = time.time() - 3600
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=1, title="Frieren", episode=5, video_path=video.resolve(), state="ready"),
        downloaded_at=stamp,
    )
    try:
        api.import_dropped_paths([str(video)])
        assert _downloaded_at(api, video) == pytest.approx(stamp)
    finally:
        api.close()


def test_legacy_dropped_row_outside_library_root_is_skipped(tmp_path: Path) -> None:
    """Rows written by earlier versions (downloaded_at set, no hash, outside the root) stay."""
    api = _api(tmp_path, progress=4)
    video, subtitle = _downloads_video(tmp_path)
    db = api.manager.db
    try:
        db.upsert_episode(
            LibraryEpisode(
                media_id=1,
                title="Frieren",
                episode=5,
                video_path=video.resolve(),
                subtitle_path=subtitle.resolve(),
                state="ready",
            ),
            downloaded_at=time.time() - 7200,
        )
        Database(api.manager.config.library.database_path).schedule_cleanup(
            video.resolve(), 0.0, media_id=1, episode=5
        )
        due = db.due_cleanup()
        assert len(due) == 1
        assert api.manager._cleanup_row_managed(due[0]) is False  # skipped as unmanaged_no_hash
        assert api.manager.cleanup() == 0
        assert video.exists() and subtitle.exists()
    finally:
        api.close()


def test_legacy_dropped_row_is_not_marked_watched_by_anilist_progress(tmp_path: Path) -> None:
    api = _api(tmp_path, progress=10)
    video, _subtitle = _downloads_video(tmp_path)
    db = api.manager.db
    try:
        db.upsert_episode(
            LibraryEpisode(media_id=1, title="Frieren", episode=5, video_path=video.resolve(), state="ready"),
            downloaded_at=time.time() - 7200,
        )
        assert api.manager._reconcile_managed_episodes_behind_anilist_progress() == 0
        assert db.episode_by_path(video.resolve()).delete_after is None
    finally:
        api.close()


def test_repair_schedule_evidence_needs_the_library_root_for_hashless_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    root = tmp_path / "Library"
    (root / "Show").mkdir(parents=True)
    db.upsert_anime(LibraryAnime(media_id=7, title="Show", progress=5, episodes=12))
    outside = tmp_path / "Downloads" / "Show - 05.mkv"
    outside.parent.mkdir()
    inside = root / "Show" / "Show - 04.mkv"
    for path in (outside, inside):
        path.write_bytes(b"video")
    for episode, path in ((5, outside), (4, inside)):
        db.upsert_episode(
            LibraryEpisode(media_id=7, title="Show", episode=episode, video_path=path, state="ready"),
            downloaded_at=time.time() - 86400,
        )
    with db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET playback_duration=1400,playback_active_seconds=1000,"
            "playback_updated_at=?,watched_at=NULL,delete_after=NULL",
            (time.time() - 90000,),
        )
    assert db.repair_missing_cleanup_schedule(2.0, library_root=root) == 1
    assert db.episode_by_path(outside).delete_after is None
    assert db.episode_by_path(inside).delete_after is not None


def test_dropped_video_without_subtitles_survives_the_next_full_scan(tmp_path: Path) -> None:
    api = _api(tmp_path, progress=0)
    video, _subtitle = _downloads_video(tmp_path, sidecar=False)
    try:
        api.import_dropped_paths([str(video)])
        row = api.manager.db.episode_by_path(video.resolve())
        assert row is not None and row.state == "waiting_subtitles"
        jobs_before = [dict(job)["video_path"] for job in api.manager.db.subtitle_jobs()]
        assert str(video.resolve()) in jobs_before
        api.manager._scan_library_uncached()
        assert api.manager.db.episode_by_path(video.resolve()) is not None
        jobs_after = [dict(job)["video_path"] for job in api.manager.db.subtitle_jobs()]
        assert str(video.resolve()) in jobs_after
        assert api.manager.dropped_media_files() == (video.resolve(),)
        # Only the file is remembered: its folder is not a watched root.
        other = video.parent / "[Group] Frieren - 06 (1080p).mkv"
        _tiny_mkv(other)
        api.manager._scan_library_uncached()
        assert api.manager.db.episode_by_path(other.resolve()) is None
    finally:
        api.close()
