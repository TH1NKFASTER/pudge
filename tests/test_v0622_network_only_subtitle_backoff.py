from __future__ import annotations

import time
from pathlib import Path

from anime_mpv.config import AppConfig
from anime_mpv.database import Database
from anime_mpv.manager import (
    AnimeManager,
    _subtitle_retry_delay_seconds,
    _subtitle_retry_is_network_error,
)
from anime_mpv.manager_models import LibraryEpisode


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.agent.subtitle_poll_minutes = 10
    return config


def test_non_network_failures_always_use_configured_interval() -> None:
    for attempts in (1, 13, 37, 500):
        assert _subtitle_retry_delay_seconds(
            poll_minutes=10,
            attempts=attempts,
            detail="Jimaku: файл нужной серии не найден с достаточной уверенностью",
        ) == 600
    assert not _subtitle_retry_is_network_error("ALASS превысил timeout 240s")
    assert not _subtitle_retry_is_network_error("7-Zip не установлен")


def test_network_failures_keep_progressive_backoff() -> None:
    assert _subtitle_retry_delay_seconds(
        poll_minutes=10,
        attempts=1,
        detail="httpx.ConnectError: temporary failure in name resolution",
    ) == 600
    assert _subtitle_retry_delay_seconds(
        poll_minutes=10,
        attempts=13,
        detail="Ошибка Jimaku API: HTTP 503",
    ) == 3600
    assert _subtitle_retry_delay_seconds(
        poll_minutes=10,
        attempts=37,
        detail="httpx.ReadTimeout: server disconnected",
    ) == 21600


def test_existing_long_backoff_is_made_due_after_upgrade(tmp_path: Path) -> None:
    config = _config(tmp_path)
    video = tmp_path / "Anime - 01.mkv"
    video.write_bytes(b"video")
    db = Database(config.library.database_path)
    db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Anime",
            episode=1,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )
    db.queue_subtitle_job(video.resolve(), 1, 1, delay_seconds=6 * 3600)
    db.set_state("subtitle_retry_generation", "older-generation")

    before = time.time()
    manager = AnimeManager(config, log=lambda _message: None)
    job = next(row for row in manager.db.subtitle_jobs() if row["video_path"] == str(video.resolve()))
    assert float(job["next_check"]) <= before + 5


def test_high_attempt_non_network_prepare_failure_retries_in_ten_minutes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = _config(tmp_path)
    manager = AnimeManager(config, log=lambda _message: None)
    video = tmp_path / "Demon Slayer - 01.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=178788,
            title="Demon Slayer",
            episode=1,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )
    manager.db.queue_subtitle_job(video.resolve(), 178788, 1)
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET attempts=50,next_check=0 WHERE video_path=?",
            (str(video.resolve()),),
        )

    class FakeProcess:
        returncode = 4

        def __init__(self, *_args, **_kwargs):
            pass

        def poll(self):
            return self.returncode

        def communicate(self):
            return (
                "Jimaku: найден архив .7z/.rar, но 7-Zip не установлен\n"
                "Jimaku: файл нужной серии не найден с достаточной уверенностью\n"
                "PREPARE_STATUS=waiting_subtitles\n",
                "",
            )

    monkeypatch.setattr("anime_mpv.manager.subprocess.Popen", FakeProcess)
    started = time.time()
    assert manager.process_subtitle_jobs(limit=1) == 0
    job = next(row for row in manager.db.subtitle_jobs() if row["video_path"] == str(video.resolve()))
    delay = float(job["next_check"]) - started
    assert 595 <= delay <= 610
