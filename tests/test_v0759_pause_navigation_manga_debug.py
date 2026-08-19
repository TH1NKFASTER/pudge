from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

from pudge.web_app import WebAppApi


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"
MANGA_JS = ROOT / "pudge/web/manga_reader_v2.js"


def test_ln_pause_keeps_live_mpv_and_exact_position() -> None:
    source = HTML.read_text(encoding="utf-8")
    toggle = source[
        source.index("async function toggleLnPairedPlayback()"):
        source.index("async function openLightNovel")
    ]

    assert "audiobook_set_paused(Number(state.audiobook_id),true)" in toggle
    assert "syncLnPairedTransportIntent(desired)" in toggle
    assert "if(!wanted)cancelLnPairedInterpolation();" in toggle
    assert "lnPairedTransportEdgePromise" in toggle
    assert "audiobook_set_paused(Number(state.audiobook_id),!wanted)" in toggle
    assert "ui.lnPairedTransportPromise" in toggle
    assert "audiobook_stop(Number(state.audiobook_id))" not in toggle
    assert "ui.lang==='ru'?'Пауза':'Pause'" in source


def test_ln_manual_navigation_delays_autofocus() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "function markLnPairedManualNavigation(reason,delayMs)" in source
    assert "markLnPairedManualNavigation('chapter',5000)" in source
    assert "markLnPairedManualNavigation('scroll',2000)" in source
    assert "manual_chapter_hold" in source
    assert "manual_scroll_hold" in source
    assert "target&&!blocked&&scroll" in source
    assert "target&&changed&&!blocked&&scroll" not in source


def test_manga_ocr_debug_frontend_contract() -> None:
    source = MANGA_JS.read_text(encoding="utf-8")

    assert 'data-manga-v2-action="ocr-debug"' not in source
    assert "async function exportMangaOcrDebug()" in source
    assert "exportDebug: exportMangaOcrDebug" in source
    assert "manga_export_ocr_debug(payload)" in source
    assert "normalized_to_image" in source
    assert "document.elementsFromPoint" in source
    assert "document.addEventListener('selectionchange'" in source
    assert "mangaDebugPoint(event, 'down')" in source
    assert "ocr_result" in source


class _FakeManga:
    def page(self, book_id: int, page_index: int) -> dict[str, object]:
        png = base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"debug-page"
        ).decode("ascii")
        return {
            "book_id": book_id,
            "page_index": page_index,
            "name": "page.png",
            "data_uri": f"data:image/png;base64,{png}",
        }

    def text_regions(
        self,
        book_id: int,
        page_index: int,
        *,
        refresh: bool,
        cached_only: bool,
    ) -> dict[str, object]:
        return {
            "book_id": book_id,
            "page_index": page_index,
            "cached": True,
            "available": True,
            "regions": [
                {
                    "text": "日本語",
                    "orientation": "vertical",
                    "x": 0.7,
                    "y": 0.2,
                    "width": 0.1,
                    "height": 0.4,
                    "confidence": 0.9,
                }
            ],
        }


def test_manga_ocr_debug_export_writes_zip_and_visual_overlay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pudge.web_app.subprocess.Popen", lambda *args, **kwargs: None)

    api = WebAppApi.__new__(WebAppApi)
    api.manga = _FakeManga()

    result = api.manga_export_ocr_debug(
        {
            "book": {"id": 7, "title": "Debug manga"},
            "page_index": 2,
            "frames": [
                {
                    "page_index": 2,
                    "overlays": [
                        {
                            "region_index": 0,
                            "text": "日本語",
                            "parsed_token_count": 1,
                            "normalized_to_image": {
                                "left": 0.68,
                                "top": 0.18,
                                "width": 0.14,
                                "height": 0.44,
                            },
                        }
                    ],
                }
            ],
            "events": [{"event": "pointer_click"}],
        }
    )

    archive = Path(result["path"])
    assert archive.is_file()
    assert "debug" in archive.parts
    assert "Downloads" not in archive.parts

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        assert {"snapshot.json", "overlay.html", "page.png"} <= names
        snapshot = json.loads(bundle.read("snapshot.json"))
        assert snapshot["backend_ocr"]["regions"][0]["text"] == "日本語"
        overlay = bundle.read("overlay.html").decode("utf-8")
        assert "Backend regions" in overlay
        assert "Rendered overlay" in overlay
        assert "B0" in overlay
        assert "F0" in overlay
