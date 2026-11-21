"""Temporary files of big writes that a quit or crash cut off are cleaned up (review of J).

Every atomic write has its own hidden temporary name now; a write killed half way leaves
it behind instead of overwriting a fixed name the next time. The cleanups of the big
writers (iPhone reading packages, segment audio, database copies, full backups) remove
such leftovers once they are an hour old; a write in progress keeps its file fresh.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from pudge import backup, fsutil
from pudge.cache_management import cleanup_segment_audio_cache
from pudge.iphone_reading import ReadingPackages

HOUR = 3600


def _file(path: Path, *, age: float, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    when = time.time() - age
    os.utime(path, (when, when))
    return path


def test_stale_temporaries_go_and_fresh_ones_and_other_files_stay(tmp_path: Path) -> None:
    old = _file(tmp_path / ".state.json.ab12.tmp", age=2 * HOUR)
    fresh = _file(tmp_path / ".state.json.cd34.tmp", age=60)
    other = _file(tmp_path / "state.json", age=2 * HOUR)
    link = tmp_path / ".link.json.ef56.tmp"
    link.symlink_to(other)
    os.utime(link, (1, 1), follow_symlinks=False)
    assert fsutil.remove_stale_temporaries(tmp_path, ".*.tmp") == 1
    assert not old.exists() and fresh.exists() and other.exists() and link.is_symlink()


def test_reading_packages_cut_off_by_a_crash_are_removed(tmp_path: Path) -> None:
    packages = ReadingPackages(SimpleNamespace(service=SimpleNamespace(root=tmp_path)))
    folder = packages.folder
    package = _file(folder / "manga-7-r1.pudgepkg", age=2 * HOUR)
    cut = _file(folder / ".manga-7-r2.pudgepkg.0a1b2c.tmp", age=2 * HOUR, data=b"x" * 4096)
    collected = _file(folder / "data-k3j4.tmp", age=2 * HOUR)
    writing = _file(folder / ".ln-3-r9.pudgepkg.9f8e7d.tmp", age=10)
    being_collected = _file(folder / "data-live.tmp", age=10)
    packages._prune()
    assert not cut.exists() and not collected.exists()
    assert package.exists() and writing.exists() and being_collected.exists()


def test_segment_audio_cleanup_removes_a_stale_extraction(tmp_path: Path) -> None:
    root = tmp_path / "segment-audio"
    cached = _file(root / "window.flac", age=2 * HOUR)
    stale = _file(root / ".window2.flac.1a2b3c.flac.tmp", age=2 * HOUR)
    running = _file(root / ".window3.flac.4d5e6f.flac.tmp", age=30)
    result = cleanup_segment_audio_cache(tmp_path, force=True)
    assert result["removed_files"] == 1
    assert cached.exists() and running.exists() and not stale.exists()


def test_snapshot_pruning_removes_stale_database_copies(tmp_path: Path) -> None:
    database = tmp_path / "library.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE t(x)")
    migration = _file(tmp_path / ".library.sqlite3.pre-v41.backup.123.456.tmp", age=2 * HOUR)
    daily = _file(tmp_path / ".library.sqlite3.daily-20261001.backup.123.tmp", age=2 * HOUR)
    running = _file(tmp_path / ".library.sqlite3.daily-20261002.backup.124.tmp", age=60)
    backup.make_database_snapshot(database, kind="daily")
    assert not migration.exists() and not daily.exists() and running.exists()


def test_a_full_backup_removes_backups_cut_off_before(tmp_path: Path) -> None:
    database = tmp_path / "data" / "library.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE t(x)")
    downloads = tmp_path / "Downloads"
    cut = _file(downloads / ".pudge-backup-20261001-101010.zip.k2j3h4.tmp", age=2 * HOUR)
    running = _file(downloads / ".pudge-backup-20261023-090000.zip.z9y8x7.tmp", age=60)
    unrelated = _file(downloads / ".holiday.zip.abc.tmp", age=2 * HOUR)
    output = downloads / "pudge-backup-20261023-120000.zip"
    backup.create_backup(
        config_path=tmp_path / "config.toml",
        database_path=database,
        cache_dir=tmp_path / "cache",
        output=output,
        version="0.0.0",
    )
    assert output.is_file()
    assert not cut.exists() and running.exists() and unrelated.exists()
