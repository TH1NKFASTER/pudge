from __future__ import annotations

import logging
import threading
import json
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "data" / "library.sqlite3"
    cfg.library.database_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.qbittorrent.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def _api(manager: AnimeManager) -> WebAppApi:
    api = WebAppApi.__new__(WebAppApi)
    api.config = manager.config
    api.manager = manager
    api.logger = logging.getLogger("v202")
    api._play_processes = {}
    api._play_started_at = {}
    api._play_exit_codes = {}
    api._play_registry = {}
    api._play_registry_path = manager.config.paths.cache_dir / "active-playbacks.json"
    api._play_lock = threading.Lock()
    return api


def _srt(path: Path, text: str = "字幕") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n",
        encoding="utf-8",
    )
    return path.resolve()


def test_missing_ready_external_is_not_exposed_as_downloaded_ready(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    anime = LibraryAnime(
        media_id=151379,
        title="Akiba Meido Sensou",
        status="PLANNING",
        progress=0,
        episodes=12,
        media_status="FINISHED",
    )
    manager.db.upsert_anime(anime)
    video1 = (manager.config.library.root_dir / "Akiba - 01.mkv").resolve()
    video2 = (manager.config.library.root_dir / "Akiba - 02.mkv").resolve()
    video1.write_bytes(b"video")
    video2.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=151379,
            title=anime.title,
            episode=1,
            video_path=video1,
            subtitle_path=(manager.config.paths.cache_dir / "missing-v12.srt").resolve(),
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    valid2 = _srt(manager.config.paths.cache_dir / "ep2-v12.srt")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=151379,
            title=anime.title,
            episode=2,
            video_path=video2,
            subtitle_path=valid2,
            subtitle_origin="jimaku",
            state="ready",
        )
    )

    payloads = _api(manager)._downloaded_payloads({151379: anime})
    assert len(payloads) == 1
    assert payloads[0]["ready_episodes"] == [2]
    assert payloads[0]["local"]["episode"] == 2


def test_completed_ready_never_skips_broken_nearest_unwatched_episode(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    anime = LibraryAnime(
        media_id=151379,
        title="Akiba Meido Sensou",
        status="PLANNING",
        progress=0,
        episodes=12,
        media_status="FINISHED",
    )
    manager.db.upsert_anime(anime)
    api = _api(manager)

    downloaded = [{
        "media_id": 151379,
        "title": anime.title,
        "ready_episodes": [2, 3, 4],
        "local": {"episode": 2, "video_path": "/tmp/ep2.mkv", "state": "ready"},
    }]
    api._downloaded_payloads = lambda _anime: downloaded
    api._pending_local_payloads = lambda _anime: []
    api._continue_payloads = lambda _anime: []
    api._is_future_unreleased = lambda _anime: False
    api._anime_payload = lambda value: {
        "media_id": value.media_id,
        "title": value.title,
        "next_episode": value.next_episode,
        "local": None,
    }
    api._deduplicate_home_sections = lambda sections: sections
    api._group_completed_ready = lambda items, _anime: items

    sections = api._home_sections([], {151379: anime})
    assert sections["completed_ready"] == []
    assert [row["media_id"] for row in sections["waiting"]] == [151379]
    assert sections["waiting"][0]["next_episode"] == 1


def test_stale_ready_integrity_repair_queues_immediate_job_and_forces_fresh_candidates(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "Akiba - 01.mkv").resolve()
    video.write_bytes(b"video")
    missing = (manager.config.paths.cache_dir / "v12-missing.srt").resolve()
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=151379,
            title="Akiba Meido Sensou",
            episode=1,
            video_path=video,
            subtitle_path=missing,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    fingerprint_key = manager._subtitle_candidate_fingerprint_state_key(video)
    manager.db.set_state(fingerprint_key, "old-candidates")

    assert manager.db.repair_stale_subtitle_selections() == 1

    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "waiting_subtitles"
    assert row.subtitle_path is None
    assert manager.db.get_state(fingerprint_key, "") == ""
    jobs = [job for job in manager.db.subtitle_jobs() if job["video_path"] == str(video)]
    assert len(jobs) == 1
    assert str(jobs[0]["state"]) == "pending"
    assert int(jobs[0]["priority"]) >= 260


def test_series_revalidation_repairs_missing_episode_and_migrates_valid_sibling(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=151379,
            title="Akiba Meido Sensou",
            progress=0,
            episodes=12,
            media_status="FINISHED",
        )
    )
    video1 = (manager.config.library.root_dir / "Akiba - 01.mkv").resolve()
    video2 = (manager.config.library.root_dir / "Akiba - 02.mkv").resolve()
    video1.write_bytes(b"video")
    video2.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=151379,
            title="Akiba Meido Sensou",
            episode=1,
            video_path=video1,
            subtitle_path=(manager.config.paths.cache_dir / "gone-v12.srt").resolve(),
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    sibling = _srt(manager.config.paths.cache_dir / "still-here-v12.srt", "二話")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=151379,
            title="Akiba Meido Sensou",
            episode=2,
            video_path=video2,
            subtitle_path=sibling,
            subtitle_origin="jimaku",
            state="ready",
        )
    )

    result = manager.revalidate_subtitle_series(151379, reason="test series integrity")

    assert result["checked"] == 2
    assert result["requeued"] == 1
    assert result["migrated"] == 1

    first = manager.db.episode_by_path(video1)
    second = manager.db.episode_by_path(video2)
    assert first is not None and first.state == "waiting_subtitles"
    assert first.subtitle_path is None
    assert second is not None and second.state == "ready"
    assert second.subtitle_path is not None
    assert second.subtitle_path.is_file()
    assert second.subtitle_path.read_text(encoding="utf-8").endswith("二話\n")
    assert second.subtitle_path.parent == manager.config.library.database_path.parent / "prepared-subtitles"
    assert second.subtitle_path != sibling

    jobs = {str(job["video_path"]): job for job in manager.db.subtitle_jobs()}
    assert str(video1) in jobs
    assert int(jobs[str(video1)]["priority"]) >= 260


def test_periodic_migration_moves_surviving_ready_cache_srt_out_of_caches(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "Old Ready.mkv").resolve()
    video.write_bytes(b"video")
    cached = _srt(manager.config.paths.cache_dir / "playback-srt" / "v12-old.srt")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=123,
            title="Old Ready",
            episode=1,
            video_path=video,
            subtitle_path=cached,
            subtitle_origin="jimaku",
            state="ready",
        )
    )

    assert manager._persist_ready_subtitle_selections() == 1
    row = manager.db.episode_by_path(video)
    assert row is not None and row.subtitle_path is not None
    assert row.subtitle_path.parent == manager.config.library.database_path.parent / "prepared-subtitles"
    assert row.subtitle_path.is_file()


def test_successful_subtitle_job_stores_durable_selection_not_cache_path(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    video = (manager.config.library.root_dir / "Prepared.mkv").resolve()
    video.write_bytes(b"video")
    cached = _srt(manager.config.paths.cache_dir / "playback-srt" / "v15-generated.srt")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=999,
            title="Prepared",
            episode=1,
            video_path=video,
            state="waiting_subtitles",
        )
    )
    manager.db.queue_subtitle_job(video, 999, 1)

    meta = json.dumps({"source": "jimaku", "name": "candidate.srt"})
    class FakeProcess:
        def __init__(self, _command, **_kwargs):
            self.returncode = 0
        def poll(self):
            return self.returncode
        def communicate(self):
            return (
                f"PREPARED_SUBTITLE={cached}\n"
                "PREPARE_STATUS=ready\n"
                f"PREPARED_SUBTITLE_META={meta}\n",
                "",
            )

    monkeypatch.setattr("pudge.manager.subprocess.Popen", FakeProcess)
    assert manager.process_subtitle_jobs(limit=1) == 1

    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "ready"
    assert row.subtitle_path is not None and row.subtitle_path.is_file()
    assert row.subtitle_path != cached
    assert row.subtitle_path.parent == manager.config.library.database_path.parent / "prepared-subtitles"
    history = manager.db.latest_selected_subtitle(video)
    assert history is not None
    details = history.get("details") if isinstance(history.get("details"), dict) else {}
    assert details.get("cache_final_path") == str(cached)
    assert details.get("final_path") == str(row.subtitle_path)
