"""DB-10: cache eviction deletes files with no library write transaction open."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pudge import cache_registry
from pudge.cache_registry import CachePolicy, CacheRegistry
from pudge.database import Database


def test_files_are_deleted_while_other_writers_can_write(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "library.sqlite3")
    root = tmp_path / "cache"
    registry = CacheRegistry(db, root)
    for index in range(4):
        entry = root / "audio" / f"entry-{index}"
        entry.mkdir(parents=True)
        (entry / "data.bin").write_bytes(b"x" * 10)
        registry.register("audio", entry)
    writable: list[bool] = []
    real_rmtree = cache_registry.shutil.rmtree

    def rmtree_probe(path, *args, **kwargs):
        other = sqlite3.connect(db.path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")  # an mpv save or a worker at this moment
            other.rollback()
            writable.append(True)
        except sqlite3.OperationalError:
            writable.append(False)
        finally:
            other.close()
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(cache_registry.shutil, "rmtree", rmtree_probe)
    result = registry.enforce({"audio": CachePolicy(max_bytes=0)})
    assert result == {"removed": 4, "removed_bytes": 40}
    assert writable == [True, True, True, True]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_registry").fetchone()[0] == 0
    assert not any((root / "audio").iterdir())


def test_an_entry_that_cannot_be_deleted_keeps_its_row(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "library.sqlite3")
    root = tmp_path / "cache"
    registry = CacheRegistry(db, root)
    entries = []
    for index in range(2):
        entry = root / "audio" / f"entry-{index}"
        entry.mkdir(parents=True)
        (entry / "data.bin").write_bytes(b"x" * 10)
        registry.register("audio", entry)
        entries.append(entry)
    real_rmtree = cache_registry.shutil.rmtree

    def failing(path, *args, **kwargs):
        if Path(path).name == "entry-0":
            raise PermissionError("busy")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(cache_registry.shutil, "rmtree", failing)
    assert registry.enforce({"audio": CachePolicy(max_bytes=0)}) == {"removed": 1, "removed_bytes": 10}
    with db.connect() as conn:
        paths = [row[0] for row in conn.execute("SELECT path FROM cache_registry")]
    assert paths == [str(entries[0].resolve())]
