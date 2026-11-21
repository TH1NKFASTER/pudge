"""A test's background dictionary fetches end with it (flaky
test_a_parsed_text_fetches_no_dictionary_entries: one ``/info`` left over from an earlier
test's word cache reached its fake Jiten). The two tests run in this order."""

from __future__ import annotations

import time
from pathlib import Path

from jiten_fake_server import install
from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    service = LightNovelService(config)
    service.save_settings({"jiten_api_key": "k", "parse_ahead": "current"})
    return service


def test_1_a_test_leaves_slow_dictionary_fetches_queued(tmp_path, monkeypatch):
    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    original = service._jiten_get

    def slow(action, params=None):
        time.sleep(0.2)
        return original(action, params)

    monkeypatch.setattr(service, "_jiten_get", slow)
    cards = [{"wordId": 900 + n, "readingIndex": 0, "spelling": f"w{n}"} for n in range(12)]
    assert len(service.jiten_words_cached(cards)) == 12
    assert server.count("/info") == 0  # all still running or queued when the test ends


def test_2_a_later_tests_fake_jiten_sees_none_of_them(tmp_path, monkeypatch):
    _clock, server = install(monkeypatch)
    service = _service(tmp_path)
    service.parse_study_text("吾輩は猫である。")
    time.sleep(0.8)  # before: the earlier test's queued fetches arrived here
    assert server.count("/info") == 0
