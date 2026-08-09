from __future__ import annotations

from pathlib import Path

from anime_mpv.config import AppConfig, load_config, write_config
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import DownloadItem, LibraryAnime
from anime_mpv.permissions import request_folder_access
from anime_mpv.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "anime_mpv" / "web" / "index.html"


def make_manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.qbittorrent.enabled = True
    write_config(cfg, cfg.config_path)
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=119321,
            title="Mahou Shoujo Madoka☆Magica: Hangyaku no Monogatari",
            format="MOVIE",
        )
    )
    return manager


def torrent(
    torrent_hash: str,
    *,
    progress: float,
    score: float,
    linked: bool = False,
    content_path: str = "",
) -> DownloadItem:
    item = DownloadItem(
        torrent_hash=torrent_hash,
        name="Mahou Shoujo Madoka Magica Hangyaku no Monogatari",
        state="downloading" if progress < 0.999 else "uploading",
        progress=progress,
        save_path="/tmp/Madoka",
        content_path=content_path or f"/tmp/Madoka/{torrent_hash}",
        media_id=119321,
        episode=None,
        is_batch=True,
        added_on=1,
        completed_on=1 if progress >= 0.999 else 0,
        raw={
            "category": "anime-mpv" if linked else "",
            "_tag_set": [
                "anime: Mahou Shoujo Madoka☆Magica: Hangyaku no Monogatari",
                "anilist: 119321",
                "series pack",
                f"score: {score}",
            ],
            "_release_score_tag": score,
        },
    )
    return item


class FakeClient:
    def __init__(self, items: list[DownloadItem]) -> None:
        self.items = items
        self.deleted: list[tuple[str, bool]] = []
        self.metadata: list[tuple[str, str, tuple[str, ...]]] = []
        self.closed = False

    def torrents(self, *, category: str = "") -> list[DownloadItem]:
        assert category == ""
        return list(self.items)

    def set_metadata(self, torrent_hash: str, *, category: str, tags: list[str]) -> None:
        self.metadata.append((torrent_hash, category, tuple(tags)))

    def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        self.deleted.append((torrent_hash, delete_files))

    def close(self) -> None:
        self.closed = True


def test_cleanup_uses_anime_tags_progress_and_score(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    better = torrent("better", progress=0.72, score=95.0, linked=True)
    duplicate = torrent("duplicate", progress=0.10, score=125.0)
    manager.db.upsert_download(better)
    client = FakeClient([better, duplicate])
    monkeypatch.setattr(manager, "qbt_client", lambda: client)

    removed = manager.cleanup_duplicate_torrents()

    assert removed == 1
    assert client.deleted == [("duplicate", True)]
    assert client.metadata and client.metadata[0][0] == "better"
    assert manager.db.download_by_hash("better") is not None
    assert client.closed is True


def test_cleanup_never_removes_completed_torrent(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    completed = torrent("complete", progress=1.0, score=70.0)
    partial = torrent("partial", progress=0.90, score=150.0)
    client = FakeClient([completed, partial])
    monkeypatch.setattr(manager, "qbt_client", lambda: client)

    assert manager.cleanup_duplicate_torrents() == 1
    assert client.deleted == [("partial", True)]
    assert all(torrent_hash != "complete" for torrent_hash, _ in client.deleted)


def test_cleanup_preserves_multiple_completed_copies(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    first = torrent("first", progress=1.0, score=80.0)
    second = torrent("second", progress=1.0, score=120.0)
    client = FakeClient([first, second])
    monkeypatch.setattr(manager, "qbt_client", lambda: client)

    assert manager.cleanup_duplicate_torrents() == 0
    assert client.deleted == []


def test_cleanup_does_not_delete_shared_content_path(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    root = str(tmp_path / "Madoka")
    winner = torrent("winner", progress=0.80, score=90.0, linked=True, content_path=root)
    loser = torrent("loser", progress=0.20, score=70.0, content_path=str(Path(root) / "part.mkv"))
    manager.db.upsert_download(winner)
    client = FakeClient([winner, loser])
    monkeypatch.setattr(manager, "qbt_client", lambda: client)

    assert manager.cleanup_duplicate_torrents() == 1
    assert client.deleted == [("loser", False)]


def test_permission_preflight_touches_folder_and_persists_flag(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.download_dirs = [tmp_path / "Downloads"]
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)

    monkeypatch.setattr(
        "anime_mpv.web_app.request_folder_access",
        lambda paths: {str(Path(path)): True for path in paths},
    )
    monkeypatch.setattr(
        "anime_mpv.web_app.request_notification_permission",
        lambda: {"supported": True, "granted": True, "error": ""},
    )

    result = api.request_permissions()

    assert result["notifications"]["granted"] is True
    assert result["settings"]["permissions_requested"] is True
    assert load_config(cfg.config_path).ui.permissions_requested is True


def test_folder_permission_probe_is_read_only_and_does_not_create_missing_folder(tmp_path: Path) -> None:
    folder = tmp_path / "Downloads"
    assert request_folder_access([folder]) == {str(folder): False}
    assert not folder.exists()

    folder.mkdir()
    assert request_folder_access([folder]) == {str(folder): True}


def test_web_app_requests_permissions_before_startup_and_offers_manual_cleanup() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "const firstPermissionRequest=!ui.state.settings.permissions_requested" in html
    assert "await pywebview.api.request_permissions()" in html
    assert "cleanupTorrentDuplicates" in html
    assert "pywebview.api.cleanup_duplicate_torrents()" in html


def test_installer_declares_protected_folder_usage_and_notification_framework() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "NSDownloadsFolderUsageDescription" in installer
    assert "NSDocumentsFolderUsageDescription" in installer
    assert "pyobjc-framework-UserNotifications" in project
