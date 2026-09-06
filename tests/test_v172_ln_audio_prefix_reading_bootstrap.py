from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from pudge.audiobooks import AudiobookService


class _Db:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


class _Response:
    status_code = 200
    content = b"{}"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "tokens": [[
                {"wordId": 1, "readingIndex": 1, "start": 0, "end": 4},
                {"wordId": 2, "readingIndex": 1, "start": 4, "end": 5},
                {"wordId": 3, "readingIndex": 1, "start": 5, "end": 8},
                {"wordId": 4, "readingIndex": 1, "start": 8, "end": 10},
            ]],
            "vocabulary": [
                {"wordId": 1, "readingIndex": 1, "reading": "小[こ]高[だか]い丘[おか]"},
                {"wordId": 2, "readingIndex": 1, "reading": "が"},
                {"wordId": 3, "readingIndex": 1, "reading": "延[えん]々[えん]と"},
                {"wordId": 4, "readingIndex": 1, "reading": "続[つづ]く"},
            ],
        }


def _service(tmp_path: Path) -> AudiobookService:
    db_path = tmp_path / "pudge.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE ln_parse_cache(text_hash TEXT PRIMARY KEY, parsed_json TEXT);
        CREATE TABLE ln_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO ln_settings(key,value) VALUES('jiten_api_key','secret-test-key');
        """
    )
    conn.close()
    service = object.__new__(AudiobookService)
    service.db = _Db(db_path)
    service.cache_dir = tmp_path / "cache"
    return service


def test_precision_alignment_bootstraps_readings_when_full_reader_cache_is_absent(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr("pudge.audiobooks.httpx.post", post)
    text = "小高い丘が延々と続く。岩ばかりが目立ち、草も木も少ない。"
    digest = "a" * 64
    hints, metadata = service._ensure_chapter_start_reading_hints(180, 0, text, digest)

    assert metadata["source"] == "jiten_prefix_api"
    assert metadata["hint_count"] >= 4
    assert hints[:4] == [
        {"offset_start": 0, "offset_end": 4, "surface": "小高い丘", "reading": "こだかいおか"},
        {"offset_start": 4, "offset_end": 5, "surface": "が", "reading": "が"},
        {"offset_start": 5, "offset_end": 8, "surface": "延々と", "reading": "えんえんと"},
        {"offset_start": 8, "offset_end": 10, "surface": "続く", "reading": "つづく"},
    ]
    assert len(calls) == 1
    assert calls[0][1]["json"]["text"][0].startswith("小高い丘")
    assert "secret-test-key" not in json.dumps(
        json.loads(service._chapter_start_reading_cache_path(digest).read_text()),
        ensure_ascii=False,
    )


def test_bootstrapped_prefix_readings_are_cached_without_second_jiten_request(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr("pudge.audiobooks.httpx.post", lambda *args, **kwargs: _Response())
    text = "小高い丘が延々と続く。"
    digest = "b" * 64
    first, first_meta = service._ensure_chapter_start_reading_hints(180, 0, text, digest)
    assert first and first_meta["source"] == "jiten_prefix_api"

    def forbidden(*args, **kwargs):
        raise AssertionError("prefix reading cache should avoid a second Jiten request")

    monkeypatch.setattr("pudge.audiobooks.httpx.post", forbidden)
    second, second_meta = service._ensure_chapter_start_reading_hints(180, 0, text, digest)
    assert second == first
    assert second_meta["source"] == "prefix_cache"
