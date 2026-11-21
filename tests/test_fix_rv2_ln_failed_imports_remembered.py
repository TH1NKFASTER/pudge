"""A light novel whose automatic import failed for good is not parsed again on every Refresh.

The library scan and the watched-folder import re-read such a file (a whole EPUB/TXT parse)
each time. A failure that stays until the file changes is now remembered by (path, size,
mtime); a missing setup, a conflict, a closed service and disk/database errors never are.
"""

from __future__ import annotations

import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from pudge.config import AppConfig, write_config
from pudge.light_novels import (
    LightNovelError,
    LightNovelImportConflict,
    LightNovelService,
    LightNovelSetupRequired,
    LightNovelUnavailable,
    permanent_import_failure,
)

BOOK = "これは試験用の日本語の文章です。物語はまだまだ続きます。" * 20


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    for folder in (cfg.library.root_dir, cfg.paths.cache_dir, cfg.library.cover_cache_dir):
        folder.mkdir(parents=True, exist_ok=True)
    return cfg


def _failing_scan(tmp_path: Path, monkeypatch, error: BaseException) -> tuple[LightNovelService, list, Path]:
    service = LightNovelService(_config(tmp_path))
    book = service.root / "Series" / "本.txt"
    book.parent.mkdir(parents=True)
    book.write_text(BOOK, encoding="utf-8")
    parses: list[Path] = []
    real_parse = service._parse_source

    def parse(source):
        parses.append(source)
        return real_parse(source)

    def failing_import(path, **_kwargs):
        raise error

    monkeypatch.setattr(service, "_parse_source", parse)
    monkeypatch.setattr(service, "import_file", failing_import)
    return service, parses, book


def test_permanent_failure_is_not_parsed_again_until_the_file_changes(tmp_path: Path, monkeypatch) -> None:
    service, parses, book = _failing_scan(tmp_path, monkeypatch, LightNovelError("unreadable"))
    assert service.scan_downloaded() == 0
    assert service.scan_downloaded() == 0
    assert len(parses) == 1
    book.write_text(BOOK + "続き。", encoding="utf-8")
    service.scan_downloaded()
    assert len(parses) == 2


@pytest.mark.parametrize(
    "error",
    [
        LightNovelSetupRequired("token"),
        LightNovelImportConflict(Path("x.txt"), {"title": "x"}),
        LightNovelUnavailable("closed"),
        OSError("busy"),
        sqlite3.OperationalError("locked"),
    ],
)
def test_temporary_failures_are_tried_again(tmp_path: Path, monkeypatch, error) -> None:
    service, parses, _book = _failing_scan(tmp_path, monkeypatch, error)
    service.scan_downloaded()
    service.scan_downloaded()
    assert len(parses) == 2


def test_classification() -> None:
    assert permanent_import_failure(zipfile.BadZipFile("bad"))
    assert permanent_import_failure(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"))
    assert permanent_import_failure(KeyError("OEBPS/missing.xhtml"))
    assert not permanent_import_failure(LightNovelSetupRequired("token"))
    assert not permanent_import_failure(FileNotFoundError("gone"))
    assert not permanent_import_failure(RuntimeError("other"))


def test_watched_folder_does_not_parse_a_failed_book_again(tmp_path: Path, monkeypatch) -> None:
    from pudge.web_app import WebAppApi

    cfg = _config(tmp_path)
    watched = tmp_path / "Downloads"
    watched.mkdir()
    book = watched / "本.txt"
    book.write_text(BOOK, encoding="utf-8")
    cfg.paths.download_dirs = [watched]
    cfg.library.recursive = True
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    checks: list[Path] = []

    def check(path):
        checks.append(path)
        return True

    def failing_import(path, **_kwargs):
        raise LightNovelError("unreadable")

    monkeypatch.setattr(api.light_novels, "is_probably_japanese_source", check)
    monkeypatch.setattr(api.light_novels, "import_file", failing_import)
    api.scan_watched_media_folders()
    api.scan_watched_media_folders()
    assert len(checks) == 1
    stat = book.stat()
    os.utime(book, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    api.scan_watched_media_folders()
    assert len(checks) == 2


def test_watched_folder_tries_a_missing_setup_again(tmp_path: Path, monkeypatch) -> None:
    from pudge.web_app import WebAppApi

    cfg = _config(tmp_path)
    watched = tmp_path / "Downloads"
    watched.mkdir()
    (watched / "本.txt").write_text(BOOK, encoding="utf-8")
    cfg.paths.download_dirs = [watched]
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    checks: list[Path] = []
    monkeypatch.setattr(
        api.light_novels, "is_probably_japanese_source", lambda path: checks.append(path) or True
    )

    def failing_import(path, **_kwargs):
        raise LightNovelSetupRequired("token")

    monkeypatch.setattr(api.light_novels, "import_file", failing_import)
    api.scan_watched_media_folders()
    api.scan_watched_media_folders()
    assert len(checks) == 2
