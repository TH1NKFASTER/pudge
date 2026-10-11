"""Pudge's own manga OCR engine is gone; Mokuro reads every page.

A one-time startup migration deletes the OCR results the old engine left (so Mokuro reads
those pages again) and its working files in the cache, and keeps everything Mokuro made
or uses. Invented pages and text only.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_mokuro import mokuro_page_regions

STATE_KEY = "manga_pudge_ocr_removed:v1"
REGION_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"


def _pudge_regions(text: str) -> list[dict]:
    # What the old engine stored: Apple Vision geometry read by MangaOCR.
    return [
        {
            "text": text,
            "raw_text": text,
            "x": 0.4,
            "y": 0.4,
            "width": 0.1,
            "height": 0.3,
            "orientation": "vertical",
            "detector": "vision-original+vision-rectangles",
            "recognizer": "manga-ocr",
            "source": "vision",
        }
    ]


def _mokuro_regions(text: str) -> list[dict]:
    block = {
        "box": [160, 120, 200, 420],
        "vertical": True,
        "font_size": 40,
        "lines": [text],
        "lines_coords": [[[160, 120], [200, 120], [200, 420], [160, 420]]],
    }
    return mokuro_page_regions({"img_width": 400, "img_height": 600, "blocks": [block]}, 400, 600, "01.png")


def _volume(tmp_path: Path, service: MangaService, name: str, pages: int) -> int:
    archive = tmp_path / f"{name}.cbz"
    Image.new("RGB", (400, 600), "white").save(tmp_path / "page.png")
    with zipfile.ZipFile(archive, "w") as zf:
        for number in range(1, pages + 1):
            zf.write(tmp_path / "page.png", f"{number:02d}.png")
    return int(service.import_file(archive)["id"])


def _publish(service: MangaService, book_id: int, pages: dict[int, list[dict]]) -> None:
    fingerprint, generation, _revision = service._ocr_context(book_id)
    assert service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[
            (index, regions, "ready" if regions else "empty_verified", "", False)
            for index, regions in pages.items()
        ],
    )


def _restart(db: Database, cache: Path) -> MangaService:
    """The app starts again after an update, with the old engine's data still there."""
    db.delete_state(STATE_KEY)
    return MangaService(db, cache_dir=cache)


def _texts(service: MangaService, book_id: int, page: int) -> list[str]:
    return [region["text"] for region in service.text_regions(book_id, page, cached_only=True)["regions"]]


def test_startup_deletes_pages_the_old_engine_read_and_keeps_mokuro_pages(tmp_path: Path, caplog) -> None:
    db, cache = Database(tmp_path / "pudge.db"), tmp_path / "cache"
    service = MangaService(db, cache_dir=cache)
    pudge_only = _volume(tmp_path, service, "old", pages=3)
    mixed = _volume(tmp_path, service, "mixed", pages=3)
    mokuro_only = _volume(tmp_path, service, "new", pages=2)
    _publish(service, pudge_only, {0: _pudge_regions("ねこ"), 1: [], 2: _pudge_regions("いぬ")})
    _publish(service, mixed, {0: _mokuro_regions("さかな"), 1: _pudge_regions("とり"), 2: []})
    _publish(service, mokuro_only, {0: _mokuro_regions("うみ"), 1: []})
    for book_id in (pudge_only, mixed, mokuro_only):
        service.ocr_artifact(book_id)
    pudge_artifact = service._ocr_artifact_path(pudge_only)
    mokuro_artifact = service._ocr_artifact_path(mokuro_only)
    assert pudge_artifact.is_file() and mokuro_artifact.is_file()
    # A row an older OCR cache generation left behind (never read any more).
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (mokuro_only, 0, "pudge-manga-regions-v78", json.dumps(_pudge_regions("ふるい")), 1.0),
        )

    with caplog.at_level(logging.INFO, logger="pudge.manga"):
        service = _restart(db, cache)

    # The old engine's volume is not read any more: Mokuro reads it again.
    assert service.ocr_cache_status(pudge_only)["completed_pages"] == 0
    assert service.ocr_cache_status(pudge_only)["not_started_pages"] == 3
    assert not pudge_artifact.exists()
    assert service.text_regions(pudge_only, 0, cached_only=True)["cached"] is False
    # A volume Mokuro started keeps its Mokuro page and its empty page; the old engine's page goes.
    assert _texts(service, mixed, 0) == ["さかな"]
    assert service.text_regions(mixed, 1, cached_only=True)["cached"] is False
    assert service.text_regions(mixed, 2, cached_only=True)["status"] == "empty_verified"
    assert service.ocr_cache_status(mixed)["completed_pages"] == 2
    # A Mokuro volume is untouched.
    assert _texts(service, mokuro_only, 0) == ["うみ"]
    assert service.ocr_cache_status(mokuro_only)["complete"] is True
    assert mokuro_artifact.is_file()
    assert [page["page_index"] for page in service.ocr_artifact(mixed)["pages"]] == [0, 2]
    with db.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM manga_ocr_cache WHERE region_key<>?", (REGION_KEY,)
            ).fetchone()[0]
            == 0
        )
    assert "manga_ocr.pudge_results_removed volumes=2 pages=4 stale_rows=1" in caplog.text

    # It runs once: a page published later in the old shape is not touched by the next start.
    _publish(service, pudge_only, {0: _pudge_regions("ねこ")})
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="pudge.manga"):
        service = MangaService(db, cache_dir=cache)
    assert _texts(service, pudge_only, 0) == ["ねこ"]
    assert "pudge_results_removed" not in caplog.text


def test_startup_deletes_only_the_old_engines_work_files(tmp_path: Path) -> None:
    db, cache = Database(tmp_path / "pudge.db"), tmp_path / "cache"
    MangaService(db, cache_dir=cache)
    root = cache / "manga-ocr"
    old = {
        "model-ready.json": "{}",
        "regions/a.png": "x",
        "batch/1.manifest.json": "{}",
        "text-detector/b.png": "x",
    }
    kept = {
        "artifacts/abc-regions-v96p27.json": "{}",
        "mokuro-worker/serve.log": "log",
        "notes.txt": "mine",
    }
    elsewhere = {
        "mokuro-ocr/venv/bin/python": "",
        "mokuro-ocr/model-ready.json": "{}",
        "covers/1.jpg": "x",
    }
    for relative, text in {**old, **kept}.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    for relative, text in elsewhere.items():
        (cache / relative).parent.mkdir(parents=True, exist_ok=True)
        (cache / relative).write_text(text, encoding="utf-8")
    # Outside the cache: a folder that a link in the old engine's place points to.
    outside = tmp_path / "outside"
    (outside / "keep.txt").parent.mkdir(parents=True)
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (root / "regions").rename(tmp_path / "regions-moved")
    (root / "regions").symlink_to(outside, target_is_directory=True)

    _restart(db, cache)

    for relative in ("model-ready.json", "regions", "batch", "text-detector"):
        assert not (root / relative).exists() and not (root / relative).is_symlink(), relative
    for relative in kept:
        assert (root / relative).is_file(), relative
    for relative in elsewhere:
        assert (cache / relative).is_file(), relative
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_startup_cleanup_with_nothing_to_remove_is_quiet(tmp_path: Path, caplog) -> None:
    db, cache = Database(tmp_path / "pudge.db"), tmp_path / "cache"
    with caplog.at_level(logging.INFO, logger="pudge.manga"):
        MangaService(db, cache_dir=cache)
    assert db.get_state(STATE_KEY, "")
    assert "manga_ocr.pudge_results_removed volumes=0 pages=0 stale_rows=0 paths=0" in caplog.text
    assert not (cache / "manga-ocr").exists()
