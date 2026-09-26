from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import companion_streaming as module
from pudge.companion_streaming import CompanionStreamingService
from pudge.manga import _validated_batch_ocr_rows


def _service(tmp_path: Path) -> CompanionStreamingService:
    return CompanionStreamingService(SimpleNamespace(), cache_dir=tmp_path)


def test_invalid_media_requests_do_not_renew_stream_ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    directory = service.cache_root / "episode"
    directory.mkdir()
    (directory / "segment-00000.ts").write_bytes(b"valid")
    ticket = service._issue_ticket(cache_key="episode", entity_id="episode", output_dir=directory)
    original = ticket.expires_at
    monkeypatch.setattr(module.time, "time", lambda: original - 10)
    with pytest.raises(ValueError, match="Invalid stream media path"):
        service.media_path(ticket.ticket, "../segment-00000.ts")
    assert ticket.expires_at == original
    with pytest.raises(FileNotFoundError):
        service.media_path(ticket.ticket, "segment-00001.ts")
    assert ticket.expires_at == original
    assert service.media_path(ticket.ticket, "segment-00000.ts")[0].is_file()
    assert ticket.expires_at > original
    service.close()


def test_hls_quota_accounts_for_protected_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    active = service.cache_root / "active"
    orphan = service.cache_root / "orphan"
    for directory in (active, orphan):
        directory.mkdir()
        (directory / "segment-00000.ts").write_bytes(b"x" * 8)
    service._issue_ticket(cache_key="active", entity_id="episode", output_dir=active)
    monkeypatch.setattr(module, "_CACHE_MAX_BYTES", 12)
    result = service.cleanup_cache()
    assert active.exists() and not orphan.exists()
    assert result["removed"] == 1
    assert result["remaining_bytes"] == 8
    service.close()


def test_hls_protected_only_still_reports_real_disk_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    active = service.cache_root / "active"
    active.mkdir()
    (active / "segment-00000.ts").write_bytes(b"x" * 14)
    service._issue_ticket(cache_key="active", entity_id="episode", output_dir=active)
    monkeypatch.setattr(module, "_CACHE_MAX_BYTES", 4)
    result = service.cleanup_cache()
    assert active.is_dir() and result["remaining_bytes"] == 14
    assert result["removed"] == 0
    service.close()


def test_ocr_batch_valid_rows_survive_bad_or_missing_siblings() -> None:
    rows, bad, errors = _validated_batch_ocr_rows(
        ['{"page_index":0,"regions":[{"text":"OK"}]}',
         '{bad json}',
         '{"page_index":5,"regions":[]}',
         '{"page_index":1,"regions":[]}',
         '{"page_index":1,"regions":[{"text":"DUPLICATE"}]}'],
        {0, 1, 2},
    )
    assert list(rows) == [0]
    assert bad == {1}
    assert "invalid_ocr_worker_row:line_2" in errors
    assert "unexpected_ocr_worker_page:5" in errors
    assert "duplicate_ocr_worker_page:1" in errors


def test_ocr_batch_rejects_invalid_regions_and_boolean_index() -> None:
    rows, bad, errors = _validated_batch_ocr_rows(
        ['{"page_index":true,"regions":[]}',
         '{"page_index":0,"regions":"not an array"}',
         '{"page_index":1,"regions":[null]}',
         '{"page_index":2,"error":"model unavailable"}',
         '{"page_index":"0","regions":[]}',
         '{"page_index":0,"error":""}'],
        {0, 1, 2},
    )
    assert rows == {2: {"page_index": 2, "error": "model unavailable"}}
    assert bad == {0, 1}
    assert len(errors) == 5


def test_batch_worker_truncation_is_retryable_and_does_not_poison_other_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import time
    import zipfile

    from PIL import Image

    from pudge.database import Database
    from pudge.manga import MangaService
    from pudge import manga as manga_module

    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as bundle:
        for index in range(2):
            image_path = tmp_path / f"{index}.png"
            Image.new("RGB", (24, 24), "white").save(image_path)
            bundle.write(image_path, arcname=f"{index}.png")
    db = Database(tmp_path / "db.sqlite3")
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(archive), "Fixture", 2, 0, "rtl", time.time(), time.time()),
        )
        book_id = int(cursor.lastrowid)

    service = MangaService(db, cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "ocr_available", lambda **_kwargs: True)
    monkeypatch.setattr(service, "_vision_text_regions", lambda _image: [])
    recover = False
    manifests: list[list[int]] = []

    class FakePopen:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: object) -> None:
            pages = json.loads(Path(command[-4]).read_text(encoding="utf-8"))["pages"]
            manifests.append([int(page["page_index"]) for page in pages])
            output = Path(command[-3])
            expected = manifests[-1]
            rows = [
                {"page_index": page, "regions": []}
                for page in (expected if recover else expected[:1])
            ]
            if not recover:
                rows.append({"page_index": 500, "regions": [{"text": "foreign"}]})
            output.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(manga_module.subprocess, "Popen", FakePopen)
    first = service.ocr_book(book_id)
    assert first["complete"] is False
    assert first["failed_pages"] == 1
    assert first["cached_pages"] == 1
    assert any("unexpected_ocr_worker_page:500" in error for error in first["errors"])
    assert service._ocr_page_status(book_id, 1)["status"] == "failed"
    assert service._ocr_page_status(book_id, 1)["retryable"] is True
    with db.connect() as conn:
        rogue = conn.execute(
            "SELECT 1 FROM manga_ocr_cache WHERE book_id=? AND page_index=500", (book_id,)
        ).fetchone()
    assert rogue is None
    recover = True
    second = service.ocr_book(book_id)
    assert manifests == [[0, 1], [1]]
    assert second["complete"] is True and second["ok"] is True
    assert second["failed_pages"] == 0
