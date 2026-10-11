"""Hand-made manga OCR edits are gone: the reader shows OCR exactly as recognized.

Earlier versions stored corrected text, reading orders, moved, hidden, added, merged
and split regions per volume. A one-time startup migration deletes them, and the
reader offers no way to make new ones. Invented pages and text only.
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
from pudge.web_app import WebAppApi
from tests.t71g06_support import reader

LEGACY_KEYS = (
    "manga_ocr_manual_corrections:v1:",
    "manga_ocr_manual_order:v1:",
    "manga_ocr_manual_geometry:v1:",
    "manga_ocr_manual_suppressions:v1:",
    "manga_ocr_manual_additions:v1:",
)


def _mokuro_regions() -> list[dict]:
    page = {
        "img_width": 400,
        "img_height": 600,
        "blocks": [
            {
                "box": [300, 100, 340, 300],
                "vertical": True,
                "font_size": 40,
                "lines": ["さかなだ"],
                "lines_coords": [[[300, 100], [340, 100], [340, 300], [300, 300]]],
            }
        ],
    }
    return mokuro_page_regions(page, 400, 600, "01.png")


def _volume(tmp_path: Path) -> tuple[Database, MangaService, int]:
    archive = tmp_path / "book.cbz"
    image = tmp_path / "page.png"
    Image.new("RGB", (400, 600), "white").save(image)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(image, "01.png")
    db = Database(tmp_path / "pudge.db")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    return db, service, int(service.import_file(archive)["id"])


def _store_legacy_edits(db: Database, book_id: int, text: str) -> None:
    anchor = {"x": 0.75, "y": 0.5, "width": 0.1, "height": 0.33, "orientation": "vertical"}
    rows = {
        "manga_ocr_manual_corrections:v1:": [
            {
                "id": "fix-1",
                "page_index": 0,
                "anchor": anchor,
                "original_text": "さかなだ",
                "corrected_text": text,
            }
        ],
        "manga_ocr_manual_order:v1:": [{"id": "order-1", "page_index": 0, "order": []}],
        "manga_ocr_manual_geometry:v1:": [{"id": "geo-1", "page_index": 0, "geometry": anchor}],
        "manga_ocr_manual_suppressions:v1:": [{"id": "hide-1", "page_index": 0, "anchor": anchor}],
        "manga_ocr_manual_additions:v1:": [
            {"id": "add-1", "page_index": 0, "kind": "addition", "text": "てがき", "geometry": anchor}
        ],
    }
    for prefix, value in rows.items():
        db.set_state(f"{prefix}{book_id}", json.dumps(value, ensure_ascii=False))


def test_startup_deletes_stored_manual_edits_once_and_keeps_other_state(tmp_path: Path, caplog) -> None:
    db, service, book_id = _volume(tmp_path)
    service.save_reader_preferences({"statusHighlight": True})
    db.set_state("manga_ocr_manual_notes_unrelated", "kept")
    # The edits of an older version are in the database when the app starts.
    _store_legacy_edits(db, book_id, "てでなおした")
    db.delete_state("manga_ocr_manual_edits_removed:v1")

    with caplog.at_level(logging.INFO, logger="pudge.manga"):
        MangaService(db, cache_dir=tmp_path / "cache")

    for prefix in LEGACY_KEYS:
        assert db.get_state(f"{prefix}{book_id}", "") == ""
    assert db.get_state("manga_ocr_manual_notes_unrelated", "") == "kept"
    assert MangaService(db, cache_dir=tmp_path / "cache").reader_preferences()["statusHighlight"] is True
    assert "manga_ocr.manual_edits_removed rows=5" in caplog.text

    # It runs once: later starts do not scan the state table again.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="pudge.manga"):
        MangaService(db, cache_dir=tmp_path / "cache")
    assert "manual_edits_removed" not in caplog.text


def test_reader_text_is_the_ocr_text_as_recognized(tmp_path: Path) -> None:
    db, service, book_id = _volume(tmp_path)
    fingerprint, generation, _revision = service._ocr_context(book_id)
    assert service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[(0, _mokuro_regions(), "ready", "", False)],
    )
    _store_legacy_edits(db, book_id, "てでなおした")
    db.delete_state("manga_ocr_manual_edits_removed:v1")
    service = MangaService(db, cache_dir=tmp_path / "cache")

    page = service.text_regions(book_id, 0, cached_only=True)
    assert [region["text"] for region in page["regions"]] == ["さかなだ"]
    assert page["regions"][0]["recognizer"] == "mokuro-import"
    assert not any(key.startswith("manual_") for key in page)
    assert not any(key.startswith("manual_") for region in page["regions"] for key in region)
    assert service.cached_region_texts(book_id) == [(0, "さかなだ")]


def test_window_has_no_ocr_editing_methods() -> None:
    for name in (
        "manga_ocr_corrections",
        "manga_set_ocr_correction",
        "manga_undo_ocr_correction",
        "manga_add_ocr_region",
        "manga_update_added_ocr_region",
        "manga_undo_added_ocr_region",
        "manga_merge_ocr_regions",
        "manga_unmerge_ocr_region",
        "manga_split_ocr_region",
        "manga_unsplit_ocr_region",
        "manga_suppress_ocr_region",
        "manga_restore_ocr_region",
        "manga_set_ocr_geometry",
        "manga_undo_ocr_geometry",
        "manga_set_ocr_reading_order",
        "manga_undo_ocr_reading_order",
    ):
        assert not hasattr(WebAppApi, name), name
    # Reading and the OCR debug export stay.
    assert hasattr(WebAppApi, "manga_text_regions") and hasattr(WebAppApi, "manga_export_ocr_debug")


def test_reader_shows_mokuro_text_without_correction_controls(page) -> None:
    regions = [
        {key: value for key, value in region.items() if key != "provenance"} for region in _mokuro_regions()
    ]
    result = reader(
        page,
        r"""
        const shown = regions().map(r => r.text);
        const settings = [...document.querySelectorAll('#mangaV2Settings [data-manga-v2-action]')].map(b => b.dataset.mangaV2Action);
        const regionMenu = await rightClick(0);
        await __fire('#mangaV2Pages .manga-v2-text-region[data-region-index="0"]', 'dblclick', {}, 'MouseEvent'); await __tick(20);
        return {shown, settings, menu: regionMenu, modal: $('modalBackdrop').classList.contains('open'),
                editors: document.querySelectorAll('#mangaReaderV2 textarea, #mangaReaderV2 [data-manga-geometry-handle]').length};
        """,
        regions={0: regions},
    )
    out = result.value
    assert out["shown"] == ["さかなだ"]
    assert "ocr-corrections" not in out["settings"] and "volume-words" in out["settings"]
    assert out["menu"] == []
    assert out["modal"] is False and out["editors"] == 0
    assert not [
        call for call in result.calls if "ocr_correction" in call["name"] or "ocr_region" in call["name"]
    ]
