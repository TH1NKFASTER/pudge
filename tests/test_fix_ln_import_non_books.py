"""Light novel imports never copy non-book text into the library: Python package metadata
(LICENSE, top_level.txt, SOURCES.txt ... of *.egg-info / *.dist-info), files of venvs,
site-packages, node_modules or .git, tiny or non-Japanese .txt files. They used to be
copied (each into a folder of its own) and only then rejected by the library scan."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pudge.config import AppConfig, write_config
from pudge.junk_files import software_path
from pudge.light_novels import LightNovelError, LightNovelService

BOOK = "これは試験用の日本語の文章です。物語はまだまだ続きます。" * 20
METADATA = {
    "LICENSE.txt": "Permission is hereby granted, free of charge, to any person obtaining a copy. " * 5,
    "top_level.txt": "examplepkg\n",
    "entry_points.txt": "[console_scripts]\nexample = examplepkg.cli:main\n",
    "requires.txt": "httpx>=0.27\n",
    "SOURCES.txt": "README.md\nexamplepkg/__init__.py\n",
    "dependency_links.txt": "\n",
}


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _library_files(service: LightNovelService) -> list[Path]:
    return [path for path in service.root.rglob("*") if path.is_file()] if service.root.exists() else []


def _books(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM ln_books").fetchone()[0])


def test_software_folders_are_recognised() -> None:
    assert software_path(Path("/u/Downloads/proj/examplepkg.egg-info/SOURCES.txt"))
    assert software_path(Path("/u/x/lib/python3.12/site-packages/a-1.0.dist-info/LICENSE.txt"))
    assert software_path(Path("/u/proj/.venv/notes.txt"))
    assert software_path(Path("/u/proj/node_modules/pkg/README.txt"))
    assert software_path(Path("/u/proj/.git/description.txt"))
    assert not software_path(Path("/u/Downloads/Novels/Vol 1/book.txt"))
    assert not software_path(Path("/u/Downloads/egg-info.txt"))  # the file name alone is no folder


def test_package_metadata_is_never_copied_even_when_dropped(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    meta = tmp_path / "Downloads" / "proj" / "examplepkg.egg-info"
    meta.mkdir(parents=True)
    for name, text in METADATA.items():
        (meta / name).write_text(text, encoding="utf-8")
    (meta / "PKG-INFO.txt").write_text(BOOK, encoding="utf-8")  # even Japanese text there
    for path in sorted(meta.iterdir()):
        with pytest.raises(LightNovelError):
            service.import_file(path, explicit=True)
    assert _library_files(service) == [] and _books(tmp_path / "pudge.sqlite3") == 0


def test_text_without_japanese_is_not_copied_when_chosen_by_hand(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    source = tmp_path / "Downloads" / "LICENSE.txt"
    source.parent.mkdir(parents=True)
    source.write_text(METADATA["LICENSE.txt"], encoding="utf-8")
    with pytest.raises(LightNovelError):
        service.import_file(source, explicit=True)
    assert _library_files(service) == []
    # A short Japanese text chosen by hand is still the user's book.
    short = tmp_path / "Downloads" / "短い話.txt"
    short.write_text("猫が好きだ。", encoding="utf-8")
    assert service.import_file(short, explicit=True)["id"]


def test_automatic_import_checks_the_text_before_copying(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    notes = tmp_path / "Downloads" / "notes"
    notes.mkdir(parents=True)
    for number in range(1, 4):
        (notes / f"{number}.txt").write_text("メモ" if number == 1 else f"note {number}\n", encoding="utf-8")
    for path in sorted(notes.iterdir()):
        with pytest.raises(LightNovelError):
            service.import_file(path, explicit=False)
    assert _library_files(service) == []
    book = tmp_path / "Downloads" / "物語.txt"
    book.write_text(BOOK, encoding="utf-8")
    assert service.import_file(book, explicit=False)["id"]
    assert len(_library_files(service)) == 1


def test_watched_folder_scan_skips_package_and_tiny_text(tmp_path: Path) -> None:
    from pudge.web_app import WebAppApi

    cfg = _config(tmp_path)
    watched = tmp_path / "Downloads"
    meta = watched / "proj" / "examplepkg.egg-info"
    meta.mkdir(parents=True)
    for name, text in METADATA.items():
        (meta / name).write_text(text, encoding="utf-8")
    (meta / "PKG-INFO.txt").write_text(BOOK, encoding="utf-8")
    venv = watched / "proj" / "venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "LICENSE.txt").write_text(BOOK, encoding="utf-8")
    for number in range(1, 19):
        (watched / "proj" / f"{number}.txt").write_text(f"note {number}", encoding="utf-8")
    (watched / "本.txt").write_text(BOOK, encoding="utf-8")
    cfg.paths.download_dirs = [watched]
    cfg.library.recursive = True
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    stats = api.scan_watched_media_folders()
    assert stats["light_novels"] == 1
    files = _library_files(api.light_novels)
    assert [path.name for path in files] == ["本.txt"]
    assert api.light_novels.scan_downloaded() == 0
    assert _books(tmp_path / "pudge.sqlite3") == 1
