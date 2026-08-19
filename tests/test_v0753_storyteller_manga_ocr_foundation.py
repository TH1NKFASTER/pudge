from __future__ import annotations

import json
import zipfile
from pathlib import Path

from PIL import Image

from pudge.alignment_quality import build_alignment_report
from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_ocr_artifact import (
    MANGA_OCR_ARTIFACT_SCHEMA,
    artifact_page,
    build_artifact,
    normalize_page,
    read_artifact,
    write_artifact,
)


def _cbz(path: Path) -> None:
    image_path = path.with_suffix(".png")
    Image.new("RGB", (120, 180), "white").save(image_path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(image_path, "001.png")
    image_path.unlink()


def test_manga_ocr_artifact_is_portable_and_versioned(tmp_path: Path) -> None:
    page = normalize_page(
        0,
        [
            {
                "text": "狼と香辛料",
                "raw_text": "狼 と 香辛料",
                "orientation": "vertical",
                "confidence": 0.91,
                "detector": "vision-contrast",
                "x": 0.7,
                "y": 0.1,
                "width": 0.1,
                "height": 0.6,
            }
        ],
        name="001.png",
        width=1200,
        height=1800,
    )
    artifact = build_artifact(
        source_fingerprint="source-1",
        title="book",
        page_count=1,
        pages=[page],
        detector="apple-vision-multipass",
        recognizer="manga-ocr",
        created_at=10.0,
    )
    target = tmp_path / "book.json"
    write_artifact(target, artifact)
    loaded = read_artifact(target)
    assert loaded is not None
    assert loaded["schema"] == MANGA_OCR_ARTIFACT_SCHEMA
    assert loaded["summary"] == {
        "processed_pages": 1,
        "region_count": 1,
        "fallback_pages": 0,
        "complete": True,
    }
    region = artifact_page(loaded, 0)["regions"][0]
    assert region["text"] == "狼と香辛料"
    assert region["raw_text"] == "狼 と 香辛料"
    assert region["id"].startswith("p0-r")


def test_manga_service_reader_prefers_artifact_over_database_cache(tmp_path: Path) -> None:
    archive = tmp_path / "book.cbz"
    _cbz(archive)
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    book = service.import_file(archive)
    book_id = int(book["id"])
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
            "VALUES(?,0,'pudge-manga-regions-v5',?,1)",
            (
                book_id,
                json.dumps(
                    [
                        {
                            "text": "artifact text",
                            "x": 0.1,
                            "y": 0.2,
                            "width": 0.3,
                            "height": 0.4,
                            "orientation": "vertical",
                        }
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
    artifact = service._rebuild_ocr_artifact(book_id)
    assert artifact["summary"]["processed_pages"] == 1
    with db.connect() as conn:
        conn.execute("DELETE FROM manga_ocr_cache WHERE book_id=?", (book_id,))
    result = service.text_regions(book_id, 0, cached_only=True)
    assert result["artifact"] is True
    assert result["regions"][0]["text"] == "artifact text"


def test_storyteller_style_report_exposes_gaps_and_unmatched_chapters() -> None:
    alignment = {
        "confidence": 0.72,
        "anchor_count": 4,
        "punctuation_pause_count": 1,
        "chapters": [
            {
                "chapter_index": 1,
                "title": "One",
                "normalized_length": 100,
                "confidence": 0.7,
                "punctuation_pause_count": 1,
                "anchors": [
                    {"offset": 0, "time": 0.0},
                    {"offset": 10, "time": 1.0},
                    {"offset": 30, "time": 5.5},
                    {"offset": 100, "time": 15.0},
                ],
            }
        ],
    }
    report = build_alignment_report(
        alignment,
        [
            {"chapter_index": 1, "title": "One", "normalized_length": 100},
            {"chapter_index": 2, "title": "Two", "normalized_length": 100},
        ],
    )
    assert report["schema"] == "pudge-alignment-report-v1"
    assert report["grade"] == "poor"
    assert report["summary"]["coverage"] == 0.5
    kinds = {warning["kind"] for warning in report["warnings"]}
    assert "long_interpolation" in kinds
    assert "unmatched_chapter" in kinds


def test_backend_exposes_reversible_alignment_and_manga_artifact_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    manga = (root / "pudge/manga.py").read_text(encoding="utf-8")
    audiobooks = (root / "pudge/audiobooks.py").read_text(encoding="utf-8")
    web = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "vision-original" in manga
    assert "vision-contrast" in manga
    assert "vision-inverted" in manga
    assert "full-page-fallback" in manga
    assert "def ocr_artifact(" in manga
    assert "pudge-alignment-pipeline-v1" in audiobooks
    assert "def alignment_report(" in audiobooks
    assert "def reprocess_alignment(" in audiobooks
    assert "clear_transcription" in audiobooks
    assert "def manga_ocr_artifact(" in web
    assert "def light_novel_audio_alignment_report(" in web
    assert "def light_novel_reprocess_audio_alignment(" in web
