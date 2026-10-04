from __future__ import annotations

import sqlite3
import threading

import pytest

from pudge.jiten_words import JitenWordRepository
from pudge.light_novels import LightNovelError, LightNovelService
from test_v088_jiten_irodori import _config


def test_retired_ln_service_rejects_late_database_access(tmp_path):
    service = LightNovelService(_config(tmp_path))
    getattr(service, "close", lambda **kwargs: None)(timeout=1)
    with pytest.raises(LightNovelError, match="closed"):
        with service._connection() as conn:
            conn.execute("SELECT 1")
    with pytest.raises(LightNovelError, match="closed"):
        service._word_repository()


def test_ln_close_waits_for_transaction_before_restore_can_replace_file(tmp_path):
    service = LightNovelService(_config(tmp_path))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def writer():
        with service._connection() as conn:
            conn.execute("CREATE TABLE restore_barrier_test(value INTEGER)")
            conn.execute("INSERT INTO restore_barrier_test VALUES(7)")
            entered.set()
            assert release.wait(2)

    def close():
        try:
            getattr(service, "close", lambda **kwargs: None)(timeout=1)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=writer)
    closer = threading.Thread(target=close)
    worker.start()
    assert entered.wait(1)
    closer.start()
    try:
        assert not finished.wait(.05), "Database replacement must wait for its active writer"
    finally:
        release.set()
        worker.join(2)
        closer.join(2)
    assert not errors
    assert finished.is_set()
    with sqlite3.connect(service.db_path) as conn:
        assert conn.execute("SELECT value FROM restore_barrier_test").fetchone() == (7,)


def test_late_dictionary_fetch_cannot_write_after_repository_is_closed(tmp_path):
    repository = JitenWordRepository(tmp_path / "words.sqlite")
    entered, release = threading.Event(), threading.Event()

    def fetch():
        entered.set()
        assert release.wait(2)
        return {"wordId": 7, "readingIndex": 0, "reading": "なな", "dictionary_complete": True}

    try:
        repository.resolve("account", {"wordId": 7}, fetch)
        assert entered.wait(1)
        getattr(repository, "close", lambda **kwargs: None)(timeout=1)
        release.set()
        repository.pool.shutdown()
        with sqlite3.connect(repository.path) as conn:
            assert conn.execute("SELECT count(*) FROM jiten_word_cache").fetchone() == (0,)
    finally:
        release.set()
        repository.pool.shutdown()
