from __future__ import annotations

import io
import types
import zipfile
from pathlib import Path

from PIL import Image

import pudge.manga as manga_module
from pudge.database import Database
from pudge.manga import MangaService

ROOT = Path(__file__).parents[1]


def _two_page_cbz(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for index, value in enumerate((20, 220), start=1):
            image = Image.new("RGB", (40, 60), (value, 0, 0))
            payload = io.BytesIO()
            image.save(payload, format="PNG")
            archive.writestr(f"{index:03}.png", payload.getvalue())


def test_manga_text_regions_are_page_specific_and_cached(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    source = tmp_path / "One Piece 2.cbz"
    _two_page_cbz(source)
    book = service.import_file(source)
    calls: list[int] = []

    monkeypatch.setattr(manga_module, "sys", types.SimpleNamespace(platform="darwin"))

    def fake_regions(image: Image.Image):
        red = int(image.getpixel((0, 0))[0])
        calls.append(red)
        return [{"text": f"頁{red}", "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}]

    monkeypatch.setattr(service, "_vision_text_regions", fake_regions)

    second = service.text_regions(book["id"], 1)
    second_again = service.text_regions(book["id"], 1)
    uncached = service.text_regions(book["id"], 0, cached_only=True)
    first = service.text_regions(book["id"], 0)

    assert second["page_index"] == 1
    assert second["regions"][0]["text"].startswith("頁")
    assert second_again["cached"] is True
    assert uncached["cached"] is False
    assert uncached["regions"] == []
    assert first["page_index"] == 0
    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_manga_reader_uses_in_place_bubble_text_and_monotonic_zoom() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
    shared = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")

    assert "manga_text_regions" in js
    assert "manga-v2-text-region" in js
    assert "manga-v2-region-content" in js
    assert "parseRegionText" in js
    assert 'id="mangaV2BubbleStudy"' not in js
    assert 'data-pudge-study-hover="1"' in js
    assert '<aside id="mangaV2OcrText"' not in js

    # Zoom is calculated from the natural image size and a positive scale.
    # Do not use WebKit CSS zoom: it produced the observed 100%+ inversion.
    assert "function sizePageImage" in js
    assert "naturalWidth * scale" in js
    assert "naturalHeight * scale" in js
    assert "zoom:var(--manga-zoom)" not in css
    assert "transform:scale(var(--manga-zoom))" not in css

    assert "data-pudge-study-hover" in shared
    assert "studyHoverTimer" in shared
    assert shared.count("document.addEventListener('pointerover'") == 1
    assert "openStudyCard" in shared



def test_manga_fullscreen_and_library_clicks_match_ln_style() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")

    assert "API()?.toggle_fullscreen" in js
    assert "def toggle_fullscreen(self)" in app
    assert "native_window.toggleFullScreen_(None)" in app
    assert "Полный экран" in js

    # Whole gray card opens the reader; secondary actions live in the same
    # right-click menu model used by Light Novels.
    assert 'data-manga-v2-action="read" data-id="${Number(continueBook.id)}"' in js
    assert 'data-manga-context-action="anilist"' in js
    assert 'data-manga-context-action="score"' in js
    assert 'data-manga-context-action="ocr-book"' in js
