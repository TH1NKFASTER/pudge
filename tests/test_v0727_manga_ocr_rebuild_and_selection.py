from __future__ import annotations

import json
from pathlib import Path

import pytest

from css_support import css_rules
from pudge.manga import _REGION_CACHE_KEY
from t71g05_support import READER, READER_SETTINGS, reader_before, vertical_segments, write_volume


# The toolbar's OCR volume button rebuilds the volume even when its OCR is complete
# (it used to accept the complete cache and finish at once), shows the run's progress
# and then the new text (behaviour; audit 71Б rewrite).

OLD = [{"text": "古い", "x": 0.4, "y": 0.4, "width": 0.1, "height": 0.2, "orientation": "vertical"}]
NEW = [{"text": "新しい", "x": 0.4, "y": 0.4, "width": 0.1, "height": 0.2, "orientation": "vertical"}]
REBUILD = r"""
window.RUN = {state: 'idle'};
const ready = {running: false, state: 'ready', complete: true, total_pages: 4, completed: 4, cached_pages: 4, processed_pages: 4};
__api.manga_ocr_book_status = async () => RUN.state === 'running'
  ? {running: true, state: 'running', complete: false, total_pages: 4, completed: 1, processed_pages: 1, cached_pages: 0, current_page: 2}
  : ready;
__api.start_manga_ocr_book = async () => { RUN.state = 'running'; return {running: true, state: 'queued', complete: false, queued: 1, total_pages: 4, cached_pages: 0}; };
__api.manga_text_regions = async (id, index) => ({book_id: id, page_index: index, available: true, cached: true,
  // A rebuild drops the volume's OCR first: nothing is cached for the page while it runs.
  regions: index || RUN.state === 'running' ? [] : (RUN.state === 'done' ? %s : %s)});
""" % (json.dumps(NEW, ensure_ascii=False), json.dumps(OLD, ensure_ascii=False))


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_ocr_volume_button_rebuilds_a_complete_volume(page, lang) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        const text = () => [...document.querySelectorAll('.manga-v2-text-region .manga-v2-region-content')].map(n => n.textContent);
        const out = {before: text()};
        void __click('[data-manga-v2-action="ocr-book"]'); await __tick(100);
        out.running = {text: text(), progress: __visible('#mangaV2OcrProgress') ? __text('#mangaV2OcrProgress') : null};
        RUN.state = 'done'; await __tick(2000);
        out.after = text();
        out.toasts = __toasts();
        return out;
        """,
        lang=lang,
        before=reader_before({}) + REBUILD,
        state=READER_SETTINGS,
    )
    out = result.value
    assert result.calls_of("start_manga_ocr_book") == [[3, True]]
    assert out["before"] == ["古い"]
    # While the volume is rebuilt the old text is not shown; the run's progress is.
    assert out["running"]["text"] == [] and out["running"]["progress"]
    assert out["after"] == ["新しい"]
    # No "Processed N pages" toast at the end (the toolbar shows the result).
    assert not [toast for toast in out["toasts"] if "Processed" in toast or "Обработалось" in toast]


def _api_with_cached_volume(tmp_path: Path, monkeypatch):
    from test_web_app import make_api

    api = make_api(tmp_path)
    book_id = int(api.manga.import_file(write_volume(tmp_path / "Volume 1.cbz"))["id"])
    with api.manga.db.connect() as conn:
        for page in (0, 1):
            conn.execute(
                "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
                (book_id, page, _REGION_CACHE_KEY, json.dumps(OLD, ensure_ascii=False), 1.0),
            )
    for page in (0, 1):
        api.manga._set_ocr_page_status(book_id, page, status="ready")
    runs: list[int] = []
    monkeypatch.setattr(api, "_manga_ocr_backend_available", lambda **_k: True)
    monkeypatch.setattr(
        api, "_run_serialized_manga_book_ocr", runs.append
    )  # the run itself is not under test
    return api, book_id, runs


@pytest.mark.parametrize("refresh", [True, False])
def test_starting_volume_ocr_with_refresh_discards_the_volumes_ocr_first(
    tmp_path: Path, monkeypatch, refresh: bool
) -> None:
    api, book_id, runs = _api_with_cached_volume(tmp_path, monkeypatch)
    try:
        assert api.manga.text_regions(book_id, 0, cached_only=True)["cached"] is True
        api.start_manga_ocr_book(book_id, refresh)
        api._manga_book_ocr_threads[book_id].join(5)
        page = api.manga.text_regions(book_id, 0, cached_only=True)
        status = api.manga.ocr_cache_status(book_id)
    finally:
        api.close()
    assert runs == [book_id]
    if refresh:
        assert page["cached"] is False and page["regions"] == []
        assert status["cached_pages"] == 0
    else:
        assert page["cached"] is True and [r["text"] for r in page["regions"]] == ["古い"]
        assert status["cached_pages"] == 2


# The text layer is selectable (copy) and the pointer shows where a click opens a word
# (behaviour; audit 71Б rewrite).

BUBBLE = {
    "text": "猫が好き",
    "x": 0.7,
    "y": 0.5,
    "width": 0.1,
    "height": 0.3,
    "orientation": "vertical",
    "source": "vision",
    "segments": vertical_segments("猫が好き", 0.7, 0.8, 0.1, 0.075),
}


def test_bubble_text_can_be_selected_and_the_cursor_marks_clickable_words(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        const region = document.querySelector('.manga-v2-text-region');
        const surface = region.querySelector('.manga-v2-selection-content');
        const hover = async (x, y) => {
          surface.dispatchEvent(new PointerEvent('pointermove', {bubbles: true, clientX: x * W, clientY: (1 - y) * H}));
          await __tick(40);
          return region.classList.contains('study-hit');
        };
        const out = {overWord: await hover(.75, .76), nearNoWord: await hover(.64, .76)};   // 12 px left of the bubble
        out.overWord2 = await hover(.75, .6);
        // A triple click selects the whole bubble for copying, not a word.
        surface.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX: 150, clientY: 66, detail: 3}));
        await __tick(50);
        out.selected = String(getSelection());
        out.bubbleSelected = region.classList.contains('bubble-selected');
        // While text in the bubble is selected, a click does not open a word card.
        surface.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX: 150, clientY: 66, detail: 1}));
        await __tick(50);
        out.cardWhileSelected = cardTerm();
        getSelection().removeAllRanges();
        out.cardAfter = await clickAt(.75, .76);
        return out;
        """,
        before=reader_before({0: [BUBBLE]}, {"猫が好き": ["猫", "が", "好き"]}),
        state=READER_SETTINGS,
    )
    out = result.value
    assert out["overWord"] is True and out["overWord2"] is True and out["nearNoWord"] is False
    assert out["selected"] == "猫が好き" and out["bubbleSelected"] is True
    assert out["cardWhileSelected"] is None
    assert out["cardAfter"] == "猫"

    rules = css_rules("manga_reader_v2.css")
    assert rules.get(".manga-v2-selection-content", "user-select") == "text"
    assert rules.get(".manga-v2-selection-content", "pointer-events") == "auto"
    assert rules.get(".manga-v2-selection-content", "cursor") == "text"
    assert rules.get(".manga-v2-text-region.study-hit .manga-v2-selection-content", "cursor") == "pointer"
