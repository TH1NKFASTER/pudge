"""Jiten budget follow-ups (independent review): a long chapter opens, hints do not hold a lease, one
failing request is not an outage.

* Opening a 63k-character chapter while background parsing had used the minute failed after two of its
  batches ("try again in 51 s"): background work now leaves 80k characters a minute to the window, and the
  open chapter's own thread waits up to 90 s (and tries again, keeping the answered batches).
* The audiobook chapter-start hints, asked for while the alignment may hold the heavy work slot, waited
  up to 300 s for the budget: now at most 5 s.
* One request that Jiten failed three times (three tries) started the outage pause for everyone: a
  request now counts once.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest


from jiten_fake_server import install
from pudge import jiten_client
from pudge.config import AppConfig
from pudge.light_novels import LightNovelService

KEY = "jiten-test-key"


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return config


def _service(tmp_path: Path) -> LightNovelService:
    service = LightNovelService(_config(tmp_path))
    service.save_settings({"jiten_api_key": KEY, "parse_ahead": "current"})
    return service


def _join(prefix: str) -> None:
    for thread in threading.enumerate():
        if thread.name.startswith(prefix):
            thread.join(timeout=10)


def _account() -> str:
    from pudge.review_providers import credential_account_key

    return credential_account_key("jiten", KEY)


PARAGRAPH = "吾輩は猫である。名前はまだ無い。" * 6


def _long_chapter(service: LightNovelService) -> int:
    text = "\n".join(f"{PARAGRAPH}{n:03d}" for n in range(650))  # 63k characters, five batches
    with service._connection() as conn:
        conn.execute(
            "INSERT INTO ln_books(id,title,file_path,file_type,created_at,updated_at) "
            "VALUES(9,'猫','/books/猫.txt','txt',0,0)"
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(9,0,'一',?,'long-63k')",
            (text,),
        )
    return 9


def _background_parsing_used_the_minute(clock, server) -> None:
    """Parse-ahead, statistics or an iPhone package used the background share this minute."""
    used = jiten_client.BACKGROUND_PARSE_CHARS_PER_MINUTE
    with jiten_client.CLIENT._state() as state:
        state["window"] = [[clock.time(), 14_000.0] for _ in range(used // 14_000)]
    server.use_parse_budget(used // 14_000 * 14_000)


def _open(service: LightNovelService, book_id: int) -> dict:
    first = service.chapter_fast(book_id, 0)
    assert first["parsing"] is True and first["tokens"] == []
    _join("ln-jiten-current")
    return service.chapter_parse_status(book_id, 0)


def test_a_long_chapter_opened_while_background_parsing_used_the_minute_gets_its_words(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    service = _service(tmp_path)
    book_id = _long_chapter(service)
    _background_parsing_used_the_minute(clock, server)
    status = _open(service, book_id)
    assert status["ready"] is True
    assert sum(1 for group in status["payload"]["tokens"] if group) == 650
    assert server.max_per_window(chars=True) <= jiten_client.PARSE_CHARS_PER_MINUTE


def test_a_long_chapter_waits_out_jitens_retry_after_on_its_own_thread(tmp_path, monkeypatch):
    clock, server = install(monkeypatch)
    service = _service(tmp_path)
    book_id = _long_chapter(service)
    server.use_parse_budget(160_000)  # the Jiten site / JitenMPV used this minute
    status = _open(service, book_id)
    assert status["ready"] is True
    assert 429 in server.statuses("reader/parse")
    # The batches answered before the 429 were not sent again.
    assert server.statuses("reader/parse").count(200) == 5
    assert max(clock.sleeps) <= service.CHAPTER_PARSE_MAX_WAIT_SECONDS


class _DB:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self):
        import sqlite3

        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def test_chapter_start_hints_do_not_wait_long_for_jiten(tmp_path, monkeypatch):
    from pudge.audiobooks import AudiobookService

    clock, server = install(monkeypatch)
    service = _service(tmp_path)
    audiobooks = AudiobookService.__new__(AudiobookService)
    audiobooks.db = _DB(service.db_path)
    audiobooks.cache_dir = tmp_path / "cache"
    server.use_parse_budget(200_000)
    jiten_client.request(
        "POST", f"{jiten_client.JITEN_API_BASE}/reader/parse", json={"text": ["猫"]}, interactive=False
    )  # a 429: parses are held back for about a minute
    started = clock.time()
    hints, meta = audiobooks._ensure_chapter_start_reading_hints(1, 0, "第一章\n吾輩は猫である。", "prefix-b")
    assert hints == [] and meta["hint_count"] == 0 and meta.get("error")
    assert clock.time() - started <= 6  # before: up to 60 s (Retry-After) or 300 s (budget)


def test_one_request_jiten_fails_three_times_counts_once_towards_the_outage_pause(monkeypatch):
    _clock, server = install(monkeypatch)
    server.fail["lookup-vocabulary"] = [500] * 9
    url = f"{jiten_client.JITEN_API_BASE}/reader/lookup-vocabulary"
    assert jiten_client.request("POST", url, json={"words": []}, attempts=3).status_code == 500
    assert server.count() == 3 and jiten_client.pause_reason() == ""
    jiten_client.request("POST", url, json={"words": []}, attempts=3)
    assert jiten_client.pause_reason() == ""
    # The third failing request in a row starts the pause: its other tries are not sent.
    with pytest.raises(jiten_client.JitenRefused):
        jiten_client.request("POST", url, json={"words": []}, attempts=3)
    assert jiten_client.pause_reason() == "jiten_unavailable" and server.count() == 7
