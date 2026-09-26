from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from pudge.backup import create_backup, restore_backup
from pudge.config import load_config
from pudge.database import Database


def _make_archive(tmp_path: Path, config_text: str) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    database_path = source / "library.sqlite3"
    Database(database_path).set_state("restore_marker", "restored")
    config_path = source / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")
    output = tmp_path / "backup.zip"
    create_backup(
        config_path=config_path, database_path=database_path,
        cache_dir=source / "cache", output=output, version="0.7.28",
    )
    return output


def _live_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    live = tmp_path / "destination"
    live.mkdir()
    database_path = live / "library.sqlite3"
    Database(database_path).set_state("restore_marker", "original")
    config_path = live / "config.toml"
    config_path.write_text(
        f'[library]\ndatabase_path = "{database_path}"\n'
        f'root_dir = "{live / "media"}"\n'
        f'[paths]\ncache_dir = "{live / "cache"}"\n'
        '[ui]\nlanguage = "en"\n',
        encoding="utf-8",
    )
    return config_path, database_path, live / "cache"


def test_restore_keeps_destination_database_and_local_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    archive = _make_archive(
        tmp_path,
        f'[library]\ndatabase_path = "{source / "missing-on-destination.sqlite3"}"\n'
        f'root_dir = "{source / "different-media"}"\n'
        f'[paths]\ncache_dir = "{source / "old-cache"}"\n'
        '[ui]\nlanguage = "ru"\n',
    )
    config, database, cache = _live_files(tmp_path)
    old_root = load_config(config).library.root_dir
    restore_backup(archive_path=archive, config_path=config, database_path=database, cache_dir=cache)
    parsed = load_config(config)
    assert parsed.library.database_path == database
    assert parsed.paths.cache_dir == cache
    assert parsed.library.root_dir == old_root
    assert parsed.ui.language == "ru"
    assert Database(parsed.library.database_path).get_state("restore_marker") == "restored"
    assert not (source / "missing-on-destination.sqlite3").exists()


@pytest.mark.parametrize("bad_config", [
    "[library\n",
    '[ui]\nreview_gate_count = "not-an-integer"\n',
    '[library]\ndatabase_path = 42\n',
])
def test_invalid_restored_config_cannot_replace_live_database_or_config(
    tmp_path: Path, bad_config: str,
) -> None:
    archive = _make_archive(tmp_path, '[ui]\nlanguage = "ru"\n')
    corrupted = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(corrupted, "w") as target:
        for name in source.namelist():
            target.writestr(name, bad_config if name == "config.toml" else source.read(name))
    config, database, cache = _live_files(tmp_path)
    original_config = config.read_bytes()
    with pytest.raises((ValueError, TypeError, KeyError)):
        restore_backup(archive_path=corrupted, config_path=config, database_path=database, cache_dir=cache)
    assert config.read_bytes() == original_config
    assert Database(database).get_state("restore_marker") == "original"


def test_runtime_rebind_failure_rolls_back_files_and_cache(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, '[ui]\nlanguage = "ru"\n')
    config, database, cache = _live_files(tmp_path)
    original_config = config.read_bytes()
    def fail_rebind() -> None:
        assert Database(database).get_state("restore_marker") == "restored"
        raise RuntimeError("simulated service initialization failure")
    with pytest.raises(RuntimeError, match="simulated service initialization"):
        restore_backup(
            archive_path=archive, config_path=config, database_path=database,
            cache_dir=cache, post_commit=fail_rebind,
        )
    assert Database(database).get_state("restore_marker") == "original"
    assert config.read_bytes() == original_config


def test_restore_recovers_when_existing_config_is_corrupt(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, '[ui]\nlanguage = "ru"\n')
    config, database, cache = _live_files(tmp_path)
    config.write_text("[bad\n", encoding="utf-8")
    restore_backup(archive_path=archive, config_path=config, database_path=database, cache_dir=cache)
    assert load_config(config).library.database_path == database
    assert Database(database).get_state("restore_marker") == "restored"
