from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


def service_at(tmp_path: Path, token: str = "token-A") -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "ln.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = token
    return LightNovelService(cfg)


def insert_book(service: LightNovelService, title: str, path: str, media_id: int | None) -> int:
    with service._connection() as conn:
        result = conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) RETURNING id",
            (title, path, "txt", 1, media_id, 100.0, 100.0),
        ).fetchone()
        return int(result[0])


def test_cold_start_preserves_anilist_groups_without_network_and_scopes_snapshot(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    first = insert_book(service, "Alpha Vol. 1", str(tmp_path / "alpha1.txt"), 123)
    second = insert_book(service, "Alpha Vol. 2", str(tmp_path / "alpha2.txt"), None)
    rows = [{"media_id": 123, "format": "NOVEL", "title": "Alpha", "status": "CURRENT", "relations": []}]
    service._save_literature_snapshot(rows, service._literature_account())
    with service._connection() as conn:
        stored = conn.execute("SELECT value FROM ln_settings WHERE key='anilist_literature_snapshot:v1'").fetchone()
        assert "token-A" not in str(stored[0])

    restarted = service_at(tmp_path)
    payload = restarted._state_payload_fast()
    assert payload["anilist"] == rows
    book_by_id = {int(book["id"]): book for book in payload["books"]}
    assert book_by_id[first]["series_key"] == book_by_id[second]["series_key"] == "anilist:123"
    assert restarted._reconcile_local_series_links() == 0
    assert service_at(tmp_path, "token-B")._anilist_cache is None
    restarted.config.anilist.access_token = "token-B"
    assert restarted._state_payload_fast()["anilist"] == []
    restarted.config.anilist.access_token = "token-A"
    assert restarted._state_payload_fast()["anilist"] == rows


def test_conflicting_manual_anilist_links_are_not_overwritten(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    first = insert_book(service, "Shared Vol. 1", str(tmp_path / "one.txt"), 101)
    second = insert_book(service, "Shared Vol. 2", str(tmp_path / "two.txt"), 202)
    third = insert_book(service, "Shared Vol. 3", str(tmp_path / "three.txt"), None)
    assert service._reconcile_local_series_links() == 0
    assert service._propagate_series_anilist(first) == 1  # only unlinked third is inherited
    assert service.book(second)["anilist_id"] == 202
    assert service.book(third)["anilist_id"] == 101
    assert service._inherit_series_anilist(second) is False
    assert service.book(second)["anilist_id"] == 202


def test_initial_refresh_error_uses_backoff_and_retries_without_erasing_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_at(tmp_path)
    rows = [{"media_id": 3, "format": "NOVEL", "title": "Offline", "status": "CURRENT"}]
    service._save_literature_snapshot(rows, service._literature_account())
    service._load_literature_snapshot()
    service._migrate_inline_covers = lambda: None
    service.scan_downloaded = lambda: 0
    service.reindex_outdated_sources = lambda: 0
    service.auto_bind_anilist = lambda: 0
    attempts: list[bool] = []

    def refresh(*, force: bool = False):
        attempts.append(force)
        if len(attempts) == 1:
            raise OSError("offline")
        return rows

    service.anilist_novels = refresh

    class InlineThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("pudge.light_novels.threading.Thread", InlineThread)
    assert service.state()["anilist"] == rows
    assert len(attempts) == 1
    assert service.state()["anilist"] == rows
    assert len(attempts) == 1  # repeated entry during backoff does not retry
    service._state_retry_after = 0.0
    assert service.state()["anilist"] == rows
    assert len(attempts) == 2
    assert service._state_retry_failures == 0
    assert service._state_last_success is not None


def test_snapshot_unavailable_viewer_does_not_erase_last_success(tmp_path: Path) -> None:
    service = service_at(tmp_path)
    rows = [{"media_id": 30, "format": "NOVEL", "title": "Saved"}]
    service._save_literature_snapshot(rows, service._literature_account())
    service._load_literature_snapshot()
    service._anilist_post = lambda *_args, **_kwargs: {"Viewer": {}}
    with pytest.raises(Exception, match="viewer unavailable"):
        service.anilist_literature(force=True)
    assert service._anilist_cache is not None
    assert service._anilist_cache[1] == rows
