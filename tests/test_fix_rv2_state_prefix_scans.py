"""The remaining ``state`` prefix reads/deletes use the key index (STATE_PREFIX_SQL).

``key LIKE 'p%'`` and ``substr(key,1,n)='p'`` read the whole table, and LIKE treats "_" as
a wildcard and ignores ASCII case: ``manga_ocr_page_status:v18:5:%`` also matched
``mangaXocr_page_status:v18:5:1``.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from pudge import watch_coverage
from pudge.consumption import ConsumptionLedger
from pudge.database import Database
from pudge.manga import MangaService


def _put(db: Database, key: str, updated_at: float = 1.0) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES(?,'{}',?)", (key, updated_at))


def _keys(db: Database) -> set[str]:
    with db.connect() as conn:
        return {row[0] for row in conn.execute("SELECT key FROM state")}


def test_forgetting_a_volumes_ocr_deletes_only_its_page_statuses(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    existing = _keys(db)
    keys = {
        "manga_ocr_page_status:v18:5:1",
        "manga_ocr_page_status:v18:5:2",
        "manga_ocr_page_status:v18:55:1",
        "mangaXocr_page_status:v18:5:1",
        "MANGA_OCR_PAGE_STATUS:v18:5:1",
    }
    for key in keys:
        _put(db, key)
    service = SimpleNamespace(_bump_ocr_generation_in_conn=lambda conn, book_id: None)
    with db.connect() as conn:
        MangaService._forget_ocr_in_conn(service, conn, 5)
    assert _keys(db) - existing == keys - {"manga_ocr_page_status:v18:5:1", "manga_ocr_page_status:v18:5:2"}


def test_deleting_history_drops_only_volume_rollups(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    for key in (
        "consumption_volume_rollup:1.000",
        "consumption_volume_rollup:2.000",
        "consumption_volume_rollupX",
    ):
        _put(db, key)
    before = _keys(db)
    ledger = SimpleNamespace()
    with db.connect() as conn:
        ConsumptionLedger._delete_history_conn(ledger, conn, new_epoch=1, reset_at=0.0)
    assert before - _keys(db) == {"consumption_volume_rollup:1.000", "consumption_volume_rollup:2.000"}


def test_watch_coverage_prunes_at_most_hourly(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    now = time.time()
    stale = now - watch_coverage.EXPIRE_SECONDS - 10
    watch_coverage.record(db, "/v/a.mkv", [(0.0, 60.0)], duration=1400.0, now=now)
    _put(db, watch_coverage.STATE_PREFIX + "/v/old.mkv", stale)
    watch_coverage.record(db, "/v/a.mkv", [(60.0, 120.0)], duration=1400.0, now=now + 5)
    assert watch_coverage.STATE_PREFIX + "/v/old.mkv" in _keys(db)  # pruned a moment ago
    later = now + watch_coverage.PRUNE_INTERVAL_SECONDS + 1
    watch_coverage.record(db, "/v/a.mkv", [(120.0, 180.0)], duration=1400.0, now=later)
    assert watch_coverage.STATE_PREFIX + "/v/old.mkv" not in _keys(db)
