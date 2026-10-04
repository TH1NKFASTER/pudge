from __future__ import annotations

import sqlite3
import time

from pudge.jiten_words import JitenWordRepository
from pudge.light_novels import LightNovelService


def test_due_card_batch_uses_one_transaction_and_preserves_lexical_details(tmp_path, monkeypatch):
    repository = JitenWordRepository(tmp_path / "words.sqlite")
    try:
        repository.put("account", {
            "wordId": 1, "readingIndex": 0, "reading": "かな",
            "all_readings": ["かな", "かん"], "dictionary_complete": True,
        })
        before, updated = repository.get("account", {"wordId": 1})
        statements = []
        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(sqlite3, "connect", connect)
        service = LightNovelService.__new__(LightNovelService)
        service._word_repository = lambda: repository
        service.study_provider_capabilities = lambda _: {"account_key": "account"}
        # Dictionary retrieval is external and asynchronous; this regression
        # exercises the real durable ingestion path before that retrieval.
        service.jiten_word = lambda card: card
        cards = [
            {"wordId": 1, "readingIndex": 0, "state": "due"},
            {"wordId": 2, "readingIndex": 0, "reading": "に", "state": "new"},
            {"wordId": 3, "readingIndex": 0, "reading": "さん"},
        ]
        assert service.cache_jiten_words(cards) == cards
        assert sum(sql == "BEGIN " for sql in statements) == 1
        assert sum(sql == "COMMIT" for sql in statements) == 1
        assert cards[0]["all_readings"] == ["かな", "かん"]
        assert cards[0]["state"] == "due"
        assert cards[0]["dictionary_complete"] is True
        persisted, refreshed = repository.get("account", cards[0])
        assert persisted["all_readings"] == before["all_readings"]
        assert refreshed == updated
        assert "state" not in persisted
        assert repository.get("another-account", cards[0]) == (None, 0)
    finally:
        repository.pool.shutdown()


def test_durable_word_cache_discards_old_rows_and_caps_newest_rows(tmp_path, monkeypatch):
    repository = JitenWordRepository(tmp_path / "words.sqlite")
    try:
        monkeypatch.setattr(repository, "MAX_PERSISTENT_ENTRIES", 3, raising=False)
        monkeypatch.setattr(repository, "RETENTION_SECONDS", 100, raising=False)
        with sqlite3.connect(repository.path) as conn:
            conn.execute("INSERT INTO jiten_word_cache VALUES(?,?,?,?,?)",
                         ("old", "id:1:0", repository.SCHEMA, '{}', time.time() - 200))
        for word_id in range(2, 6):
            repository.put("account", {"wordId": word_id, "readingIndex": 0,
                                       "reading": "かな"})
        with sqlite3.connect(repository.path) as conn:
            keys = {row[0] for row in conn.execute("SELECT key FROM jiten_word_cache")}
        assert keys == {"id:3:0", "id:4:0", "id:5:0"}
        assert repository.get("old", {"wordId": 1}) == (None, 0)
        assert repository.get("account", {"wordId": 5})[0]["reading"] == "かな"
    finally:
        repository.pool.shutdown()
