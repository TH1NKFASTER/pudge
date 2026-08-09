from pathlib import Path

from anime_mpv.config import AppConfig
from anime_mpv.manager import AnimeManager


def make_manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    return AnimeManager(cfg, log=lambda _message: None)


def stub_regular_maintenance(monkeypatch, manager: AnimeManager) -> None:
    monkeypatch.setattr(manager, "_requeue_legacy_generated_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "_requeue_after_resolver_upgrade", lambda: 0)
    monkeypatch.setattr(manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(manager, "cleanup_duplicate_torrents", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda: [])
    monkeypatch.setattr(manager, "refresh_anilist_if_due", lambda: 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: 0)
    monkeypatch.setattr(manager, "auto_upgrade_downloaded", lambda: 0)
    monkeypatch.setattr(manager, "finalize_ready_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "reconcile_duplicate_versions", lambda: 0)
    monkeypatch.setattr(manager, "cleanup", lambda: 0)
    monkeypatch.setattr(manager, "enforce_disk_limit", lambda: 0)


def test_manual_refresh_resets_six_hour_subtitle_backoff(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    stub_regular_maintenance(monkeypatch, manager)
    video = tmp_path / "Mushoku Tensei III - 06.mkv"
    video.write_bytes(b"video")
    manager.db.queue_subtitle_job(video, 178789, 6, delay_seconds=6 * 60 * 60)
    assert manager.db.due_subtitle_jobs(limit=10) == []

    observed: dict[str, int] = {}

    def process(*, limit: int = 4) -> int:
        observed["limit"] = limit
        observed["due"] = len(manager.db.due_subtitle_jobs(limit=100))
        return observed["due"]

    monkeypatch.setattr(manager, "process_subtitle_jobs", process)
    stats = manager.run_once(force_subtitle_retry=True)

    assert observed == {"limit": 8, "due": 1}
    assert stats["subs"] == 1


def test_regular_run_keeps_future_subtitle_backoff(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    stub_regular_maintenance(monkeypatch, manager)
    video = tmp_path / "Mushoku Tensei III - 06.mkv"
    video.write_bytes(b"video")
    manager.db.queue_subtitle_job(video, 178789, 6, delay_seconds=6 * 60 * 60)

    observed: dict[str, int] = {}

    def process(*, limit: int = 4) -> int:
        observed["limit"] = limit
        observed["due"] = len(manager.db.due_subtitle_jobs(limit=100))
        return observed["due"]

    monkeypatch.setattr(manager, "process_subtitle_jobs", process)
    manager.run_once()

    assert observed == {"limit": 8, "due": 0}
