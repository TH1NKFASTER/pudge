"""WEBFLOW-8: the foreground poll's completed-download reconcile reruns only on a
change, never walks a save folder and never refiles a file under another title."""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode

MEDIA = 210031


def _manager(tmp_path: Path, monkeypatch) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(media_id=MEDIA, title="Show", episodes=24, status="CURRENT"))
    return manager


def _download(manager: AnimeManager, number: int, *, with_row: bool = True, content: bool = True) -> Path:
    folder = manager.config.library.root_dir / "Show"
    folder.mkdir(exist_ok=True)
    video = folder / f"[SubsPlease] Show - {number:02d} (1080p).mkv"
    if content:
        video.write_bytes(b"video")
    torrent_hash = f"{number:040x}"
    if with_row and content:
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=MEDIA,
                title="Show",
                episode=number,
                media_episode=number,
                release_episode=number,
                video_path=video.resolve(),
                state="ready",
                torrent_hash=torrent_hash,
            )
        )
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash=torrent_hash,
            name=video.name,
            state="complete",
            progress=1.0,
            save_path=str(folder),
            content_path=str(video.resolve()) if content else "",
            media_id=MEDIA,
            episode=number,
            media_episode=number,
            release_episode=number,
            completed_on=10,
        )
    )
    return video


@pytest.fixture
def lookups(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = AnimeManager._completed_download_video_files.__func__

    def counting(cls, item, **kwargs):
        seen.append(item.torrent_hash)
        return real(cls, item, **kwargs)

    monkeypatch.setattr(AnimeManager, "_completed_download_video_files", classmethod(counting))
    return seen


def test_an_unchanged_library_costs_nothing_on_the_next_polls(tmp_path: Path, monkeypatch, lookups) -> None:
    manager = _manager(tmp_path, monkeypatch)
    for number in range(1, 13):
        _download(manager, number)
    assert manager.reconcile_completed_download_rows_from_poll() == 0
    first = len(lookups)
    assert first >= 12
    for _ in range(5):
        assert manager.reconcile_completed_download_rows_from_poll() == 0
    assert len(lookups) == first  # before: 2 lookups per completed download on every poll


def test_a_new_completion_is_materialized_by_the_next_poll(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    _download(manager, 1)
    manager.reconcile_completed_download_rows_from_poll()
    video = _download(manager, 2, with_row=False)
    assert manager.db.episode_by_path(video.resolve()) is None
    assert manager.reconcile_completed_download_rows_from_poll() >= 1
    row = manager.db.episode_by_path(video.resolve())
    assert row is not None and row.media_id == MEDIA and row.media_episode == 2


def test_the_poll_never_walks_a_save_folder(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    _download(manager, 1)
    _download(manager, 5, content=False)  # entry gone: the folder used to be walked every poll
    for number in range(30):
        (manager.config.library.root_dir / "Show" / f"other {number}.mkv").write_bytes(b"x")
    walked: list[str] = []
    real = Path.rglob

    def counting(self, pattern):
        walked.append(str(self))
        return real(self, pattern)

    monkeypatch.setattr(Path, "rglob", counting)
    manager.reconcile_completed_download_rows_from_poll()
    assert walked == []
    manager.reconcile_completed_download_rows()  # the full reconcile (after a scan) still may
    assert walked


def test_the_poll_does_not_refile_another_titles_file(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    manager.db.upsert_anime(LibraryAnime(media_id=999, title="Other", episodes=12, status="CURRENT"))
    video = _download(manager, 3, with_row=False)
    manager.db.upsert_episode(
        LibraryEpisode(media_id=999, title="Other", episode=3, video_path=video.resolve(), state="ready")
    )
    manager.reconcile_completed_download_rows_from_poll()
    assert manager.db.episode_by_path(video.resolve()).media_id == 999


def test_foreground_poll_uses_the_gated_reconcile(tmp_path: Path, monkeypatch) -> None:
    from pudge.config import write_config
    from pudge.web_app import WebAppApi

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    calls: list[str] = []
    monkeypatch.setattr(
        api.manager, "reconcile_completed_download_rows_from_poll", lambda: calls.append("poll") or 0
    )
    monkeypatch.setattr(api.manager, "reconcile_completed_download_rows", lambda: calls.append("full") or 0)
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(api.manager, "scan_subtitle_inbox", lambda: {"requeued": 0})
    api._poll_downloads_and_subtitles_sync("", False, False)
    assert calls == ["poll"]
