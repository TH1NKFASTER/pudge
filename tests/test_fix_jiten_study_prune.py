"""Pruning old Jiten study texts (independent review follow-up).

It deleted jpdb chapter parses (their account-scoped keys are no chapter hash) and, past 50k rows, the
bubbles of manga volumes still in the library. Now only Jiten one-off study texts are pruned.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path


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


def test_pruning_keeps_jpdb_parses_and_the_bubbles_of_manga_in_the_library(tmp_path):
    service = LightNovelService(_config(tmp_path))
    old = time.time() - 400 * 86400
    bubble = "吾輩は猫である。"
    bubble_key = hashlib.sha256(("study-v1\0" + bubble).encode("utf-8")).hexdigest()
    with service._connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS manga_ocr_cache(book_id INTEGER,page_index INTEGER,region_key TEXT,"
            "text TEXT,updated_at REAL)"
        )
        conn.execute("INSERT INTO manga_ocr_cache VALUES(1,0,'r1',?,0)", (bubble,))
        rows = [
            ("jpdb-chapter", "jpdb-v1", old),  # a jpdb chapter parse (account-scoped key)
            (bubble_key, "jiten-v1", old),  # a bubble of a volume still in the library
            ("one-off-old", "jiten-v1", old),  # a VN line nobody needs any more
            ("batch-old", "jiten-batch-v1", old),  # a batch left by an unfinished chapter
            ("recent-a", "jiten-v1", time.time() - 2),
            ("recent-b", "jiten-v1", time.time() - 1),
        ]
        for key, schema, used in rows:
            conn.execute(
                "INSERT INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at,used_at) VALUES(?,?,?,?,?)",
                (key, json.dumps({}), schema, used, used),
            )
    service.STUDY_PARSE_KEEP_ROWS = 1  # a "large library": the cap is far below what is kept
    assert service.prune_study_parses() == 3
    with service._connection() as conn:
        left = {row[0] for row in conn.execute("SELECT text_hash FROM ln_parse_cache")}
    assert left == {"jpdb-chapter", bubble_key, "recent-b"}
