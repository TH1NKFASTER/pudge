from __future__ import annotations

import json
import time
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
from pudge.web_app import WebAppApi
from t71g23_support import paired_book
from tests.t71g06_support import bridge


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
    service = MangaService(db, cache_dir=tmp_path / "cache")
    book = service.import_file(archive)
    book_id = int(book["id"])
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
            "VALUES(?,0,'pudge-manga-regions-v96p27-orphan-vertical-ink',?,1)",
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


# ---------------------------------------------------------------- the window API


def test_manga_ocr_artifact_through_the_window_api(tmp_path: Path) -> None:
    archive = tmp_path / "book.cbz"
    _cbz(archive)
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    book_id = int(service.import_file(archive)["id"])
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
            "VALUES(?,0,'pudge-manga-regions-v96p27-orphan-vertical-ink',?,1)",
            (
                book_id,
                json.dumps(
                    [
                        {
                            "text": "狼",
                            "x": 0.1,
                            "y": 0.2,
                            "width": 0.3,
                            "height": 0.4,
                            "orientation": "vertical",
                        }
                    ]
                ),
            ),
        )
    api = bridge(service)
    artifact = api.manga_ocr_artifact(book_id)
    assert artifact["schema"] == MANGA_OCR_ARTIFACT_SCHEMA
    assert artifact["summary"]["processed_pages"] == 1
    assert artifact_page(artifact, 0)["regions"][0]["text"] == "狼"
    # Built once, then served from the portable file.
    with db.connect() as conn:
        conn.execute("DELETE FROM manga_ocr_cache WHERE book_id=?", (book_id,))
    assert artifact_page(api.manga_ocr_artifact(book_id), 0)["regions"][0]["text"] == "狼"


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_alignment_report_and_reversible_reprocess_through_the_window_api(
    tmp_path: Path, monkeypatch
) -> None:
    audio, ln_id, audio_id = paired_book(tmp_path, [("One", "狼と商人の物語です。")], duration=120.0)
    api = WebAppApi.__new__(WebAppApi)
    api.audiobooks = audio
    assert api.light_novel_audio_alignment_report(ln_id) == {"status": "not_prepared", "ready": False}
    assert api.light_novel_audio_alignment_report(ln_id + 1) == {"status": "unlinked", "ready": False}

    # One alignment run (speech recognition and the aligner replaced).
    transcript = audio._transcript_path(audio_id)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps({"segments": [{"start": 0.0, "end": 2.0, "text": "狼と商人"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(
        audio, "_load_transcript", lambda _id: json.loads(transcript.read_text(encoding="utf-8"))
    )
    monkeypatch.setattr(audio, "_load_or_analyze_activity", lambda *_a, **_k: ([], "test"))
    monkeypatch.setattr(audio, "_cached_chapter_start_reading_hints", lambda _ln: {})
    monkeypatch.setattr(audio, "_alignment_chapter_needs_precision", lambda _row: False)
    chapter = {
        "chapter_index": 0,
        "title": "One",
        "start": 0.0,
        "end": 100.0,
        "confidence": 0.4,
        "anchors": [{"offset": 0, "time": 0.0}, {"offset": 2, "time": 1.0}, {"offset": 10, "time": 30.0}],
    }
    monkeypatch.setattr(
        audio,
        "_align_reading_audio",
        lambda *_a, **_k: {
            "schema": "reading-audio-v3",
            "chapters": [chapter],
            "confidence": 0.4,
            "anchor_count": 3,
        },
    )
    audio.prepare_alignment(ln_id)
    alignment, report_file = (
        audio._alignment_path(ln_id, audio_id),
        audio._alignment_report_path(ln_id, audio_id),
    )
    _wait_until(lambda: alignment.is_file() and report_file.is_file())
    stored = json.loads(alignment.read_text(encoding="utf-8"))
    # The run records how it was made, and that its transcription can be reused.
    assert stored["processing"]["schema"] == "pudge-alignment-pipeline-v1"
    assert stored["processing"]["transcription_reusable"] is True
    report = api.light_novel_audio_alignment_report(ln_id)
    assert (report["status"], report["ready"], report["schema"]) == (
        "ready",
        True,
        "pudge-alignment-report-v1",
    )
    assert "long_interpolation" in {warning["kind"] for warning in report["warnings"]}

    # Reprocessing starts over from the text alignment and keeps the transcription,
    # unless asked to drop it too.
    prepared: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        audio,
        "prepare_alignment",
        lambda ln, force=False: prepared.append((ln, force)) or {"status": "queued"},
    )
    assert api.light_novel_reprocess_audio_alignment(ln_id) == {"status": "queued"}
    assert (alignment.exists(), report_file.exists(), transcript.exists()) == (False, False, True)
    api.light_novel_reprocess_audio_alignment(ln_id, True)
    assert transcript.exists() is False
    assert prepared == [(ln_id, False), (ln_id, False)]
