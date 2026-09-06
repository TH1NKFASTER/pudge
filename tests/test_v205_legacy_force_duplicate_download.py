from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.qbittorrent.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def test_pre_v204_missing_ready_history_is_upgraded_to_force_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "Akiba - 01.mkv").resolve()
    video.write_bytes(b"video")
    missing = (manager.config.paths.cache_dir / "playback-srt" / "v12-gone.srt").resolve()

    # This is the state left by v203: the Ready row has already been invalidated,
    # so there is no current subtitle_path and no v204 force marker.  The only
    # durable evidence is the successful selection in subtitle history.
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=151379,
            title="Akiba Meido Sensou",
            episode=1,
            video_path=video,
            state="waiting_subtitles",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=151379,
        episode=1,
        source="jimaku",
        candidate_name="same.ass",
        candidate_path=tmp_path / "raw.ass",
        status="selected",
        details={"final_path": str(missing)},
    )
    manager.db.set_state(manager._subtitle_candidate_fingerprint_state_key(video), "same")
    manager.db.queue_subtitle_job(video, 151379, 1, priority=260)

    prepared = (manager.config.paths.cache_dir / "playback-srt" / "v15-new.srt").resolve()
    prepared.parent.mkdir(parents=True, exist_ok=True)
    prepared.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    commands: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            commands.append(list(command))
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return (
                f"PREPARED_SUBTITLE={prepared}\n"
                "SUBTITLE_CANDIDATE_FINGERPRINT=same\n"
                "PREPARE_STATUS=ready\n"
                'PREPARED_SUBTITLE_META={"source":"jimaku","name":"same.srt"}\n',
                "",
            )

    monkeypatch.setattr(manager.work_scheduler, "background_allowed", lambda **_kwargs: True)
    monkeypatch.setattr(manager, "_notify_ready_episode", lambda *_a, **_k: None)
    monkeypatch.setattr("pudge.manager.subprocess.Popen", FakeProcess)

    assert manager.process_subtitle_jobs(limit=1) == 1
    assert commands
    command = commands[0]
    assert "--force-search" in command
    assert "--resync" in command
    assert "--previous-candidate-fingerprint" not in command
    assert manager.db.get_state(manager._subtitle_force_rebuild_state_key(video), "") == ""
    assert manager.db.subtitle_jobs() == []


def test_duplicate_completed_download_hashes_do_not_ping_pong_ready_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=187260, title="KimiShinu", episodes=12))
    folder = manager.config.library.root_dir / "KimiShinu"
    folder.mkdir()
    video = (folder / "[SubsPlease] KimiShinu - 09 (1080p).mkv").resolve()
    video.write_bytes(b"video")
    subtitle = (tmp_path / "prepared.srt").resolve()
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")

    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=187260,
            title="KimiShinu",
            episode=9,
            media_episode=9,
            release_episode=9,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
            torrent_hash="hash-a",
        )
    )
    for torrent_hash in ("hash-a", "hash-b"):
        manager.db.upsert_download(
            DownloadItem(
                torrent_hash=torrent_hash,
                name=video.name,
                state="complete",
                progress=1.0,
                save_path=str(folder),
                content_path=str(video),
                media_id=187260,
                episode=9,
                media_episode=9,
                release_episode=9,
                completed_on=10,
            )
        )

    # Re-registration would probe the file and may queue another subtitle job;
    # a matching local episode must short-circuit before that path.
    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_source",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("unexpected reprobe")),
    )

    assert manager.reconcile_completed_download_rows(187260, 9) == 0
    assert manager.reconcile_completed_download_rows(187260, 9) == 0
    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "ready"
    assert row.subtitle_path == subtitle
    assert row.torrent_hash == "hash-a"
    assert manager.db.subtitle_jobs() == []
