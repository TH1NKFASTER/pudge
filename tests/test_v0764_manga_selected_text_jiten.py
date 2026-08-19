from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_reading_tools_can_open_study_card_from_selected_text() -> None:
    js = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert "async function openStudyText(text, rect = null" in js
    assert "API()?.study_parse_text" in js
    assert "firstStudyTokenFromPayload" in js
    assert "openText: openStudyText" in js


def test_manga_prefers_real_selection_before_ocr_token_geometry() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    handle = js[js.index("async function handleMangaImageStudyClick"):js.index("document.addEventListener('click', async event =>", js.index("async function handleMangaImageStudyClick"))]
    assert "openMangaSelectedText(pointerSelection" in handle
    assert "openMangaSelectedText(liveSelection" in handle
    assert "openMangaSelectedText(mangaLastSelection" in handle
    assert handle.index("openMangaSelectedText(pointerSelection") < handle.index("mangaRegionAtPoint")
    assert "jiten_selection_open" in js
    assert "jiten_selection_failed" in js


def test_selection_click_requires_pointer_near_selected_rect() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "pointNearRect(event.clientX, event.clientY, candidate.rect, 22)" in js
    assert "mangaPointerSelection = image ? currentJapaneseSelection(image) : null" in js
    assert "performance.now() - Number(mangaLastSelection.at || 0) < 2500" in js
