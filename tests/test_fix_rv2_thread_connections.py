"""The window's library keeps one SQLite connection per thread between repository calls.

Every fresh connection parses the library's large schema (milliseconds): the phone server
authenticated each request on a new one. Measured (container, 500 authenticate calls):
1.38 ms -> 0.02 ms per call. At most a few connections stay parked in the whole process.
"""

from __future__ import annotations

import os
import sqlite3
import threading

import pytest

from pudge import database
from pudge.database import Database


def _parked(db: Database) -> list[sqlite3.Connection]:
    with database._PARKED_LOCK:
        return [h.conn for h in database._PARKED.values() if h.owner is db._park_owner and h.conn]


def _counting(monkeypatch) -> list[int]:
    opened: list[int] = []
    real = Database._open_connection

    def counting(db):
        opened.append(1)
        return real(db)

    monkeypatch.setattr(Database, "_open_connection", counting)
    return opened


def test_calls_on_one_thread_share_one_connection(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "lib.sqlite3", thread_connections=True)
    opened = _counting(monkeypatch)
    for index in range(20):
        db.set_state(f"k{index}", str(index))
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'k%'").fetchone()[0] == 20
    assert len(opened) == 0  # the connection initialization left parked


def test_helpers_still_open_one_connection_per_call(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "lib.sqlite3")
    opened = _counting(monkeypatch)
    for index in range(5):
        db.set_state(f"k{index}", "v")
    assert len(opened) == 5
    assert db._parked_count() == 0


def test_failed_call_rolls_back_and_the_connection_stays_clean(tmp_path) -> None:
    db = Database(tmp_path / "lib.sqlite3", thread_connections=True)
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            conn.execute("INSERT INTO state(key,value,updated_at) VALUES('lost','x',0)")
            raise RuntimeError("boom")
    with db.connect() as conn:
        assert not conn.in_transaction
        assert conn.execute("SELECT 1 FROM state WHERE key='lost'").fetchone() is None


def test_retire_closes_parked_connections(tmp_path) -> None:
    db = Database(tmp_path / "lib.sqlite3", thread_connections=True)
    db.set_state("a", "1")
    parked = _parked(db)
    assert len(parked) == 1
    conn = parked[0]
    db.retire(timeout=1.0)
    assert db._parked_count() == 0
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    with pytest.raises(RuntimeError):
        db.set_state("b", "2")


def test_a_replaced_library_file_is_opened_again(tmp_path) -> None:
    path = tmp_path / "lib.sqlite3"
    db = Database(path, thread_connections=True)
    db.set_state("which", "old")
    with db.connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    (stale,) = _parked(db)
    other = tmp_path / "other.sqlite3"
    Database(other).set_state("which", "new")
    os.replace(other, path)
    with db.connect() as conn:
        assert conn.execute("SELECT value FROM state WHERE key='which'").fetchone()[0] == "new"
        assert conn is not stale
    with pytest.raises(sqlite3.ProgrammingError):
        stale.execute("SELECT 1")


def test_parked_connections_are_bounded_in_the_process(tmp_path) -> None:
    db = Database(tmp_path / "lib.sqlite3", thread_connections=True)
    others = [Database(tmp_path / f"other{n}.sqlite3", thread_connections=True) for n in range(3)]
    workers = [
        threading.Thread(target=lambda n=n: db.set_state(f"worker{n}", "1"))
        for n in range(database._PARKED_LIMIT + 5)
    ]
    for worker in workers:
        worker.start()
        worker.join()
    with database._PARKED_LOCK:
        assert len(database._PARKED) <= database._PARKED_LIMIT
    # The oldest were closed; their threads (or Databases) open a new connection if used again.
    for other in others:
        other.set_state("again", "1")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'worker%'").fetchone()[0] == len(
            workers
        )


def test_nested_calls_get_their_own_connection(tmp_path) -> None:
    db = Database(tmp_path / "lib.sqlite3", thread_connections=True)
    db.set_state("x", "1")
    with db.connect() as outer:
        with db.connect() as inner:
            assert inner is not outer
            inner.execute("UPDATE state SET value='2' WHERE key='x'")
        assert outer.execute("SELECT value FROM state WHERE key='x'").fetchone()[0] == "2"
    with db.connect() as conn:
        assert conn.execute("SELECT value FROM state WHERE key='x'").fetchone()[0] == "2"


def test_a_connection_with_a_live_cursor_is_not_parked(tmp_path) -> None:
    # An unfinished SELECT keeps the old read snapshot: the next call would read stale rows.
    db = Database(tmp_path / "lib.sqlite3", thread_connections=True)
    for index in range(3):
        db.set_state(f"row{index}", "x")
    with db.connect() as conn:
        cursor = conn.execute("SELECT key FROM state")
        cursor.fetchone()
    Database(tmp_path / "lib.sqlite3", initialize=False).set_state("other", "1")
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM state WHERE key='other'").fetchone() is not None
    del cursor
