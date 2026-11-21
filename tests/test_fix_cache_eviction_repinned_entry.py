"""Wave-4 follow-up to DB-10: an entry re-pinned (or used) between the eviction's decision
and its deletion keeps its file and row (reviewer repro rev7)."""

from __future__ import annotations

from pathlib import Path

from pudge.cache_registry import CachePolicy, CacheRegistry
from pudge.database import Database


def _entry(tmp_path: Path) -> tuple[Database, Path, Path, CacheRegistry]:
    db = Database(tmp_path / "lib.sqlite3")
    root = tmp_path / "cache"
    entry = root / "intro" / "a.json"
    entry.parent.mkdir(parents=True)
    entry.write_text("x" * 100)
    registry = CacheRegistry(db, root)
    registry.register("intro", entry, expires_at=1.0)  # expired: a victim
    return db, root, entry, registry


def test_eviction_does_not_drop_an_entry_repinned_meanwhile(tmp_path: Path, monkeypatch) -> None:
    db, root, entry, registry = _entry(tmp_path)
    real_connect = Database.connect
    state = {"decided": False}

    def racing_connect(self):
        # The second connection of enforce() is the delete: another worker (window vs
        # agent) re-registers the entry pinned between the decision and the delete.
        if state["decided"] is False and self is db:
            state["decided"] = None
        elif state["decided"] is None and self is db:
            state["decided"] = True
            CacheRegistry(Database(tmp_path / "lib.sqlite3", initialize=False), root).register(
                "intro", entry, pinned=True
            )
        return real_connect(self)

    monkeypatch.setattr(Database, "connect", racing_connect)
    registry.enforce({"intro": CachePolicy(max_bytes=10_000)})
    monkeypatch.setattr(Database, "connect", real_connect)
    with db.connect() as conn:
        rows = conn.execute("SELECT pinned FROM cache_registry").fetchall()
    assert rows and int(rows[0][0]) == 1 and entry.exists()


def test_an_entry_registered_again_after_the_row_delete_keeps_its_file(tmp_path: Path, monkeypatch) -> None:
    db, root, entry, registry = _entry(tmp_path)
    real_connect = Database.connect
    calls = {"n": 0}

    def racing_connect(self):
        if self is db:
            calls["n"] += 1
            if calls["n"] == 3:  # decide, delete rows, then the check before the file goes
                CacheRegistry(Database(tmp_path / "lib.sqlite3", initialize=False), root).register(
                    "intro", entry, pinned=True
                )
        return real_connect(self)

    monkeypatch.setattr(Database, "connect", racing_connect)
    assert registry.enforce({"intro": CachePolicy(max_bytes=10_000)}) == {"removed": 0, "removed_bytes": 0}
    monkeypatch.setattr(Database, "connect", real_connect)
    assert entry.exists()
    with db.connect() as conn:
        assert conn.execute("SELECT pinned FROM cache_registry").fetchone()[0] == 1


def test_an_untouched_victim_still_goes(tmp_path: Path) -> None:
    db, _root, entry, registry = _entry(tmp_path)
    assert registry.enforce({"intro": CachePolicy(max_bytes=10_000)}) == {"removed": 1, "removed_bytes": 100}
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_registry").fetchone()[0] == 0
    assert not entry.exists()
