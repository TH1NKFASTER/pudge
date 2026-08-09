from __future__ import annotations

import time
from pathlib import Path

import httpx

from anime_mpv.config import AppConfig
from anime_mpv.database import Database
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from anime_mpv.providers.qbittorrent import QBittorrentClient


def make_manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.anilist.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def test_new_completed_subtitle_job_wins_priority(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    old = tmp_path / "old.mkv"
    new = tmp_path / "new.mkv"
    db.queue_subtitle_job(old, 1, 1, priority=0)
    db.queue_subtitle_job(new, 2, 2, priority=100)

    claimed = db.claim_due_subtitle_jobs(1)

    assert len(claimed) == 1
    assert claimed[0]["video_path"] == str(new)
    assert claimed[0]["priority"] == 100


def test_completed_download_queues_high_priority_job(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=177699, title="Koukaku"))
    video = manager.config.library.root_dir / "Koukaku - 05.mkv"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        "anime_mpv.manager.japanese_subtitle_source",
        lambda *args, **kwargs: ("none", None),
    )

    completed_paths: list[Path] = []
    count = manager._register_completed_download(
        DownloadItem(
            torrent_hash="abc",
            name=video.name,
            state="uploading",
            progress=1.0,
            save_path=str(video.parent),
            content_path=str(video),
            media_id=177699,
            episode=5,
            completed_on=int(time.time()),
        ),
        completed_paths=completed_paths,
    )

    jobs = manager.db.subtitle_jobs()
    assert count == 1
    assert completed_paths == [video.resolve()]
    assert len(jobs) == 1
    assert jobs[0]["priority"] == 100


def test_qbittorrent_cleanup_removes_score_and_all_unused_tags() -> None:
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.append((request.url.path, body))
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "abc",
                        "name": "Anime - 01",
                        "state": "uploading",
                        "progress": 1.0,
                        "save_path": "/tmp",
                        "content_path": "/tmp/Anime - 01.mkv",
                        "tags": "anime: Anime, anilist: 1, episode: 1, score: 100.0",
                    }
                ],
            )
        if request.url.path == "/api/v2/torrents/removeTags":
            return httpx.Response(200, text="")
        if request.url.path == "/api/v2/torrents/tags":
            return httpx.Response(
                200,
                json=[
                    "anime: Anime",
                    "anilist: 1",
                    "episode: 1",
                    "score: 100.0",
                    "old empty tag",
                ],
            )
        if request.url.path == "/api/v2/torrents/deleteTags":
            return httpx.Response(200, text="")
        raise AssertionError(request.url.path)

    client = QBittorrentClient("http://qbt.local", api_key="test")
    client.client.close()
    client.client = httpx.Client(
        base_url="http://qbt.local",
        transport=httpx.MockTransport(handler),
    )

    result = client.cleanup_tags()
    client.close()

    assert result == {"score_tags_removed": 1, "unused_tags_deleted": 2}
    remove_body = next(body for path, body in seen if path.endswith("removeTags"))
    delete_body = next(body for path, body in seen if path.endswith("deleteTags"))
    assert b"score%3A+100.0" in remove_body
    assert b"old+empty+tag" in delete_body
    assert b"score%3A+100.0" in delete_body


def test_cleanup_repairs_missing_hash_before_deleting_koukaku(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    manager.config.qbittorrent.enabled = True
    manager.db.upsert_anime(
        LibraryAnime(media_id=177699, title="Koukaku", progress=5, episodes=12)
    )
    video = manager.config.library.root_dir / (
        "[Erai-raws] Koukaku Kidoutai (2026) - 05 "
        "[1080p AMZN WEB-DL AVC EAC3][MultiSub][ADDB49AE].mkv"
    )
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=177699,
            title="Koukaku",
            episode=5,
            video_path=video,
            state="watched",
            torrent_hash="",
            watched_at=time.time() - 7200,
            delete_after=time.time() - 60,
        ),
        downloaded_at=time.time() - 86400,
    )
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash="abc",
            name=video.name,
            state="uploading",
            progress=1.0,
            save_path=str(video.parent),
            content_path=str(video),
            media_id=177699,
            episode=5,
            completed_on=int(time.time() - 86400),
        )
    )

    class FakeClient:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, bool]] = []

        def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
            self.deleted.append((torrent_hash, delete_files))

        def close(self) -> None:
            pass

    client = FakeClient()
    monkeypatch.setattr(manager, "qbt_client", lambda: client)

    assert manager.cleanup() == 1
    assert client.deleted == [("abc", True)]
    assert not video.exists()
    assert manager.db.episode_by_path(video) is None
