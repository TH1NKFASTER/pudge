from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from anime_mpv.config import AppConfig
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryAnime, LibraryEpisode
from anime_mpv.web_app import WebAppApi


def make_manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.paths.cache_dir = tmp_path / "Library" / "Caches" / "pudge"
    cfg.library.database_path = tmp_path / "data" / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies" / "pudge"
    cfg.library.cover_cache_dir = cfg.paths.cache_dir / "covers"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return AnimeManager(cfg, log=lambda _message: None)


def test_brand_rename_recovers_prepared_subtitle_from_history(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    anime = LibraryAnime(media_id=100723, title="Boku no Hero Academia THE MOVIE: Futari no Hero", format="MOVIE")
    manager.db.upsert_anime(anime)

    video = manager.config.library.root_dir / "Boku.no.Hero.Academia.Two.Heroes.1080p.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=100723, title=anime.title, episode=None, video_path=video, state="waiting_subtitles")
    )
    manager.db.queue_subtitle_job(video, 100723, None)

    current_subtitle = manager.config.paths.cache_dir / "playback-srt" / "two-heroes.srt"
    current_subtitle.parent.mkdir(parents=True, exist_ok=True)
    current_subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    legacy_subtitle = manager.config.paths.cache_dir.parent / "anime-mpv" / "playback-srt" / current_subtitle.name
    legacy_video = tmp_path / "Movies" / "Anime MPV" / video.name

    manager.db.record_subtitle_history(
        video_path=legacy_video,
        media_id=None,  # reproduces the pre-v0.6.58 lost movie identity
        episode=None,
        source="jimaku",
        candidate_name="two-heroes.srt",
        candidate_path=legacy_subtitle,
        status="selected",
        reason="Preparation completed",
        details={"final_path": str(legacy_subtitle), "source": "jimaku"},
    )

    assert manager._repair_brand_moved_subtitle_selections() == 1
    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.state == "ready"
    assert row.subtitle_path == current_subtitle.resolve()
    assert manager.db.subtitle_jobs() == []


def test_manual_refresh_requests_blocking_maintenance_lock(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    seen: list[bool] = []

    @contextmanager
    def fake_lock(_cache_dir: Path, *, blocking: bool = False):
        seen.append(blocking)
        yield True

    monkeypatch.setattr("anime_mpv.manager.maintenance_lock", fake_lock)
    monkeypatch.setattr(manager, "_run_once_unlocked", lambda **_kwargs: {"auto": 0})

    assert manager.run_once(force_subtitle_retry=True, wait_for_maintenance=True) == {"auto": 0}
    assert seen == [True]


def test_manual_refresh_searches_nyaa_before_long_subtitle_work(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    order: list[str] = []

    monkeypatch.setattr(manager, "_repair_brand_moved_subtitle_selections", lambda: 0)
    monkeypatch.setattr(manager.db, "repair_bitmap_ready_rows", lambda: 0)
    monkeypatch.setattr(manager.db, "repair_spurious_ready_subtitle_jobs", lambda: 0)
    monkeypatch.setattr(manager.db, "repair_stale_subtitle_selections", lambda: 0)
    monkeypatch.setattr(manager, "invalidate_disabled_ocr_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "_requeue_legacy_generated_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "_requeue_after_resolver_upgrade", lambda: 0)
    monkeypatch.setattr(manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(manager, "cleanup_duplicate_torrents", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda: [])
    monkeypatch.setattr(manager, "scan_subtitle_inbox", lambda: {})
    monkeypatch.setattr(manager, "repair_library_if_due", lambda: {})
    monkeypatch.setattr(manager, "schedule_subtitle_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "_clear_jimaku_api_cache", lambda: 0)
    monkeypatch.setattr(manager.db, "force_requeue_unresolved_subtitle_jobs", lambda **_kwargs: 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: order.append("nyaa") or 0)
    monkeypatch.setattr(manager, "process_subtitle_jobs", lambda *, limit=4: order.append("subtitles") or 0)
    monkeypatch.setattr(manager, "refresh_anilist_if_due", lambda: 0)
    monkeypatch.setattr(manager, "auto_upgrade_downloaded", lambda: 0)
    monkeypatch.setattr(manager, "finalize_ready_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "reconcile_duplicate_versions", lambda: 0)
    monkeypatch.setattr(manager, "cleanup", lambda: 0)
    monkeypatch.setattr(manager, "enforce_disk_limit", lambda: 0)
    monkeypatch.setattr(manager, "cleanup_qbittorrent_tags", lambda: {})

    manager._run_once_unlocked(force_subtitle_retry=True, prioritize_release_search=True)
    assert order[:2] == ["nyaa", "subtitles"]


def test_web_refresh_uses_interactive_maintenance(tmp_path: Path) -> None:
    calls: list[str] = []

    class Manager:
        def run_interactive_refresh(self):
            calls.append("interactive")
            return {"auto": 0}

    api = WebAppApi.__new__(WebAppApi)
    api.manager = Manager()
    api.logger = logging.getLogger("test-v0659-web-refresh")
    api.config = SimpleNamespace(qbittorrent=SimpleNamespace(enabled=False), aria2=SimpleNamespace(enabled=False))
    api._startup_maintenance_thread = None
    api._local_refresh_lock = threading.Lock()
    api.get_state = lambda: {"home": {}}

    result = api.refresh_local()
    assert result["skipped"] is False
    assert calls == ["interactive"]


def _write_srt(path: Path, end_seconds: int) -> None:
    minutes, seconds = divmod(end_seconds, 60)
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nstart\n\n"
        f"2\n00:{minutes:02d}:{max(0, seconds-1):02d},000 --> 00:{minutes:02d}:{seconds:02d},000\nend\n",
        encoding="utf-8",
    )


def test_alass_structure_does_not_blame_existing_long_movie_sign_cues(tmp_path: Path) -> None:
    from anime_mpv.syncing import _validate_embedded_reference_output

    reference = tmp_path / "reference.srt"
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    _write_srt(reference, 100)
    _write_srt(source, 400)
    _write_srt(aligned, 405)

    ok, reason, _details = _validate_embedded_reference_output(source, aligned, reference)
    assert ok is True
    assert reason == "ok"


def test_alass_structure_still_rejects_output_that_itself_grows_too_long(tmp_path: Path) -> None:
    from anime_mpv.syncing import _validate_embedded_reference_output

    reference = tmp_path / "reference.srt"
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    _write_srt(reference, 100)
    _write_srt(source, 100)
    _write_srt(aligned, 400)

    ok, reason, _details = _validate_embedded_reference_output(source, aligned, reference)
    assert ok is False
    assert reason == "aligned_too_long"
