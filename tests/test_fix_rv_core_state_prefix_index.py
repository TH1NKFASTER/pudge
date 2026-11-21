from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pudge import watch_coverage
from pudge.database import STATE_PREFIX_SQL, Database, state_prefix_bounds
from pudge.download_intents import DownloadIntentStore


def _put(db: Database, key: str, value: str, updated_at: float) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES(?,?,?)", (key, value, updated_at))


def _keys(db: Database) -> set[str]:
    with db.connect() as conn:
        return {row[0] for row in conn.execute("SELECT key FROM state")}


def test_bounds_select_exactly_the_prefix(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    now = time.time()
    for key in ("abc:", "abc:1", "abc:zz\U0001f600", "abc;", "abc", "abb:9", "ABC:1", "abcX1"):
        _put(db, key, "v", now)
    with db.connect() as conn:
        rows = conn.execute(f"SELECT key FROM state WHERE {STATE_PREFIX_SQL}", state_prefix_bounds("abc:"))
        assert {row[0] for row in rows} == {"abc:", "abc:1", "abc:zz\U0001f600"}
    with pytest.raises(ValueError):
        state_prefix_bounds("")


def test_prefix_queries_search_the_key_index(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    with db.connect() as conn:
        plan = conn.execute(
            f"EXPLAIN QUERY PLAN DELETE FROM state WHERE {STATE_PREFIX_SQL} AND updated_at<?",
            (*state_prefix_bounds(watch_coverage.STATE_PREFIX), 0.0),
        ).fetchall()
    detail = " ".join(str(row[3]) for row in plan)
    assert "SEARCH state USING INDEX" in detail
    assert "SCAN state" not in detail


def test_download_intents_ignore_keys_that_only_matched_like_wildcards(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    payload = json.dumps({"state": "waiting", "owned_candidates": ["a" * 40], "stall_candidates": []})
    now = time.time()
    _put(db, "download_intent:7:episode:3", payload, now)
    # "_" is a LIKE wildcard: this unrelated key was read as a download intent before.
    _put(db, "downloadXintent:9:episode:1", payload, now)

    store = DownloadIntentStore(db)

    assert len(store.recoverable()) == 1
    assert store.waiting_count() == 1


def test_watch_coverage_prune_removes_only_expired_coverage(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    now = time.time()
    old = now - watch_coverage.EXPIRE_SECONDS - 10
    _put(db, watch_coverage.STATE_PREFIX + "/videos/old.mkv", "{}", old)
    _put(db, "watch_intervals:v2:/videos/other.mkv", "{}", old)
    _put(db, "unrelated", "{}", old)
    before = _keys(db)

    watch_coverage.record(db, "/videos/new.mkv", [(0.0, 120.0)], duration=1400.0, now=now)

    assert _keys(db) == (before - {watch_coverage.STATE_PREFIX + "/videos/old.mkv"}) | {
        watch_coverage.STATE_PREFIX + "/videos/new.mkv"
    }


def test_suffix_range_on_an_empty_file_is_not_satisfiable() -> None:
    from pudge.iphone_http import parse_range

    assert parse_range(None, 0) == (0, -1)
    for value in ("bytes=-5", "bytes=0-", "bytes=0-0"):
        with pytest.raises(ValueError):
            parse_range(value, 0)


def test_charset_detector_is_imported_only_when_needed() -> None:
    import subprocess
    import sys

    code = (
        "import sys, pudge.language as l\n"
        "assert 'charset_normalizer' not in sys.modules\n"
        "assert l.decode_subtitle_bytes('テスト'.encode('utf-8')) == 'テスト'\n"
        "assert 'charset_normalizer' not in sys.modules\n"
        # Not UTF-8 / EUC-JP / Shift-JIS / cp932: only the detector can read it.
        "text = l.decode_subtitle_bytes('Привет, как дела? Это проверка.'.encode('cp1251'))\n"
        "assert 'charset_normalizer' in sys.modules\n"
        "assert isinstance(text, str) and text\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).resolve().parents[1])
