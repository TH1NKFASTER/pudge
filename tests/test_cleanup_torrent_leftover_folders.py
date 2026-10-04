"""Anime folders holding only download leftovers (.torrent/.anilist.id/.aria2) are removed."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig
from pudge.manager import AnimeManager


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    return AnimeManager(cfg)


def _folder(root: Path, name: str, files: dict[str, bytes], *, age: float = 24 * 3600) -> Path:
    folder = root / name
    folder.mkdir()
    old = time.time() - age
    for rel, data in files.items():
        path = folder / rel
        path.write_bytes(data)
        os.utime(path, (old, old))
    return folder


def test_leftover_only_folders_are_removed_and_real_content_kept(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    root = manager.config.library.root_dir
    stale = _folder(root, "Grand Blue Season 3", {"a" * 40 + ".torrent": b"t", ".anilist.id": b"1", "x.mkv.aria2": b"a"})
    corrupt = _folder(root, "Kimi ga Shinu", {"b" * 40 + ".torrent": b"t", "ep.mkv.pudge-corrupt-1788688551": b"c", ".anilist.id": b"2"})
    video = _folder(root, "Sayonara Lara", {"c" * 40 + ".torrent": b"t", "ep01.mkv": b"v"})
    books = _folder(root, "Light Novels", {"book.epub": b"e"})
    fresh = _folder(root, "Just Added", {"d" * 40 + ".torrent": b"t", ".anilist.id": b"3"}, age=60)
    active = _folder(root, "Fetching Metadata", {"e" * 40 + ".torrent": b"t", ".anilist.id": b"4"})
    monkeypatch.setattr(manager, "incomplete_download_paths", lambda: ())
    monkeypatch.setattr(manager.db, "downloads", lambda: [SimpleNamespace(torrent_hash="e" * 40)])
    monkeypatch.setattr(manager, "_download_is_complete", lambda item: False)
    monkeypatch.setattr(manager.db, "anime_list", lambda *a, **k: [SimpleNamespace(media_id=i) for i in (1, 2, 3, 4)])

    manager._prune_empty_library_dirs()

    assert not stale.exists()
    assert corrupt.exists(), "a quarantined file may be the only remaining copy"
    assert video.exists() and books.exists()
    assert fresh.exists(), "recently touched leftovers are kept"
    assert active.exists(), "metadata of an unfinished download is kept"


def test_folder_of_an_unfinished_download_is_kept(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    root = manager.config.library.root_dir
    folder = _folder(root, "Downloading", {"f" * 40 + ".torrent": b"t", ".anilist.id": b"5"})
    monkeypatch.setattr(manager, "incomplete_download_paths", lambda: (folder / "ep01.mkv",))
    monkeypatch.setattr(manager.db, "downloads", lambda: [])
    manager._prune_empty_library_dirs()
    assert folder.exists()


def _known(manager, monkeypatch, ids=(7,)):
    monkeypatch.setattr(manager, "incomplete_download_paths", lambda: ())
    monkeypatch.setattr(manager.db, "downloads", lambda: [])
    monkeypatch.setattr(manager.db, "anime_list", lambda *a, **k: [SimpleNamespace(media_id=i) for i in ids])


def test_user_torrent_folder_without_ownership_is_kept(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    root = manager.config.library.root_dir
    _known(manager, monkeypatch)
    personal = _folder(root, "my-saved-torrents", {"my-personal.torrent": b"t"})
    named = _folder(root, "Show With Marker", {"my-personal.torrent": b"t", ".anilist.id": b"7"})
    unknown_owner = _folder(root, "Other Title", {"a" * 40 + ".torrent": b"t", ".anilist.id": b"999"})
    manager._prune_empty_library_dirs()
    assert (personal / "my-personal.torrent").exists()
    assert (named / "my-personal.torrent").exists(), "non-aria2 .torrent names are the user's"
    assert (unknown_owner / ("a" * 40 + ".torrent")).exists(), "marker must name a library title"


def test_proven_pudge_leftovers_are_removed(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    root = manager.config.library.root_dir
    _known(manager, monkeypatch)
    folder = _folder(root, "Owned", {"a" * 40 + ".torrent": b"t", ".anilist.id": b"7", "ep.mkv.aria2": b"x"})
    manager._prune_empty_library_dirs()
    assert not folder.exists()


def test_unreadable_library_state_never_deletes(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    root = manager.config.library.root_dir
    _known(manager, monkeypatch)

    def broken(*_a, **_k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(manager.db, "anime_list", broken)
    folder = _folder(root, "Owned", {"a" * 40 + ".torrent": b"t", ".anilist.id": b"7"})
    manager._prune_empty_library_dirs()
    assert folder.exists()
