from __future__ import annotations

import logging
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

from anime_mpv.config import AppConfig
from anime_mpv.database import Database
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryAnime, LibraryEpisode
from anime_mpv.providers.qbittorrent import QBittorrentClient
from anime_mpv.web_app import WebAppApi


def test_qbittorrent_start_uses_modern_endpoint() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/start"):
            assert b"hashes=abc" in request.read()
            return httpx.Response(200, text="")
        raise AssertionError(request.url.path)

    client = QBittorrentClient("http://qbt.local", api_key="test")
    client.client.close()
    client.client = httpx.Client(
        base_url="http://qbt.local",
        transport=httpx.MockTransport(handler),
    )
    client.start("abc")
    client.close()

    assert seen == ["/api/v2/torrents/start"]


def test_qbittorrent_start_falls_back_to_legacy_resume() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/start"):
            return httpx.Response(404)
        if request.url.path.endswith("/resume"):
            return httpx.Response(200, text="")
        raise AssertionError(request.url.path)

    client = QBittorrentClient("http://qbt.local", api_key="test")
    client.client.close()
    client.client = httpx.Client(
        base_url="http://qbt.local",
        transport=httpx.MockTransport(handler),
    )
    client.start("abc")
    client.close()

    assert seen == ["/api/v2/torrents/start", "/api/v2/torrents/resume"]


def test_manual_refresh_reconciles_new_torrent_in_same_click() -> None:
    calls: list[str] = []

    class Manager:
        def run_interactive_refresh(self):
            calls.append("run")
            return {"auto": 1}

        def sync_downloads(self):
            calls.append("sync")
            return 1

        def log(self, _message: str):
            pass

    api = WebAppApi.__new__(WebAppApi)
    api.manager = Manager()
    api.config = SimpleNamespace(qbittorrent=SimpleNamespace(enabled=True))
    api.logger = logging.getLogger("test-first-refresh")
    api._startup_maintenance_thread = None
    import threading

    api._local_refresh_lock = threading.Lock()
    api.get_state = lambda: {"downloads": ["visible"]}

    result = api.refresh_local()

    assert calls == ["run", "sync"]
    assert result["stats"]["downloads_after_auto"] == 1
    assert result["state"]["downloads"] == ["visible"]


def test_schedule_cleanup_falls_back_to_media_and_episode(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=177699, title="Koukaku", progress=5))
    actual = tmp_path / "actual" / "Koukaku - 05.mkv"
    actual.parent.mkdir()
    actual.write_bytes(b"video")
    db.upsert_episode(
        LibraryEpisode(
            media_id=177699,
            title="Koukaku",
            episode=5,
            video_path=actual,
            state="ready",
            torrent_hash="abc",
        ),
        downloaded_at=time.time() - 3600,
    )

    stale_tracking_path = tmp_path / "old-location" / "Koukaku - 05.mkv"
    assert db.schedule_cleanup(
        stale_tracking_path,
        0,
        media_id=177699,
        episode=5,
    ) == 1

    row = db.episode_by_path(actual)
    assert row is not None
    assert row.state == "watched"
    assert row.delete_after is not None


def test_repair_missing_cleanup_schedule_from_playback_evidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(
        LibraryAnime(media_id=177699, title="Koukaku", progress=5, episodes=12)
    )
    video = tmp_path / "Koukaku - 05.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(
        LibraryEpisode(
            media_id=177699,
            title="Koukaku",
            episode=5,
            video_path=video,
            state="ready",
            torrent_hash="abc",
        ),
        downloaded_at=time.time() - 86400,
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET playback_duration=1400,playback_active_seconds=1000,"
            "playback_updated_at=?,watched_at=NULL,delete_after=NULL WHERE video_path=?",
            (time.time() - 90000, str(video)),
        )

    assert db.repair_missing_cleanup_schedule(2.0) == 1
    repaired = db.episode_by_path(video)
    assert repaired is not None
    assert repaired.state == "watched"
    assert repaired.watched_at is not None
    assert repaired.delete_after is not None
    assert repaired.delete_after <= time.time()


def test_cleanup_repairs_and_deletes_overdue_episode(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.anilist.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.agent.delete_only_managed_files = True
    cfg.agent.delete_after_watched_hours = 2.0
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=177699, title="Koukaku", progress=5, episodes=12)
    )
    video = cfg.library.root_dir / "Koukaku - 05.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=177699,
            title="Koukaku",
            episode=5,
            video_path=video,
            state="ready",
            torrent_hash="",
        ),
        downloaded_at=time.time() - 172800,
    )
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET playback_duration=1400,playback_active_seconds=1000,"
            "playback_updated_at=? WHERE video_path=?",
            (time.time() - 100000, str(video)),
        )

    assert manager.cleanup() == 1
    assert not video.exists()


def test_filename_uses_season_and_title_from_parent_directories(tmp_path: Path) -> None:
    from anime_mpv.filename import parse_anime_filename

    video = tmp_path / "Otomege Sekai wa Mob ni Kibishii Sekai desu" / "Season 02" / "Episode 05.mkv"
    identity = parse_anime_filename(video)

    assert identity.title == "Otomege Sekai wa Mob ni Kibishii Sekai desu"
    assert identity.season == 2
    assert identity.episode == 5


def test_library_uses_anilist_sidecar_before_fuzzy_title_matching(tmp_path: Path) -> None:
    from anime_mpv.library import scan_library

    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=159309, title="Otomege season two"))
    root = tmp_path / "library"
    folder = root / "Completely ambiguous folder"
    folder.mkdir(parents=True)
    (folder / ".anilist.id").write_text("159309\n", encoding="utf-8")
    video = folder / "Episode 05.mkv"
    video.write_bytes(b"video")

    episodes = scan_library(root, db, ffprobe="missing-ffprobe")

    assert len(episodes) == 1
    assert episodes[0].media_id == 159309
    assert episodes[0].episode == 5
