"""The page the user is looking at gets real OCR when its cached OCR is missing, instead
of waiting for the whole-volume run; other visible or preloaded pages only read the
cache (behaviour, page harness; audit 71Б rewrite of the v0.7.27 visible-page and
cold-cache foreground OCR tests)."""

from __future__ import annotations

import json

from t71g05_support import READER, READER_SETTINGS, reader_before

CAT = [{"text": "猫", "x": 0.4, "y": 0.4, "width": 0.1, "height": 0.1, "orientation": "vertical"}]


def pages(answers: dict[int, dict]) -> str:
    """manga_text_regions answers per page: {"cached": {...}, "ocr": {...}}; "cached" answers
    a cached-only read, "ocr" a real read of the page."""
    return r"""
window.PAGES = %s;
__api.manga_text_regions = async (id, index, refresh, cachedOnly) => {
  const page = PAGES[index] || {};
  return {book_id: id, page_index: index, regions: [], ...(cachedOnly ? page.cached : page.ocr)};
};
""" % json.dumps({str(k): v for k, v in answers.items()}, ensure_ascii=False)


MISS = {"available": True, "cached": False}
READ = {"available": True, "cached": False, "regions": CAT, "status": "ready"}


def reads(result, page: int) -> list[str]:
    return [
        "cached" if args[3] else "ocr" for args in result.calls_of("manga_text_regions") if args[1] == page
    ]


def test_visible_page_with_a_cold_cache_is_read_now_and_shown(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        const out = {text: __text('.manga-v2-text-region .manga-v2-region-content')};
        await __key('L', {code: 'KeyL', metaKey: true, shiftKey: true}); await __tick(20);
        return out;
        """,
        before=reader_before({})
        + pages({0: {"cached": MISS, "ocr": READ}})
        + "__api.manga_export_ocr_debug = async () => ({path: '/logs/ocr.json'});",
        state=READER_SETTINGS,
    )
    assert reads(result, 0) == ["cached", "ocr"]
    assert result.value["text"] == "猫"
    # The OCR debug log says why the page was read in the foreground.
    events = result.calls_of("manga_export_ocr_debug")[0][0]["events"]
    assert [(e["page_index"], e["reason"]) for e in events if e["event"] == "ocr_foreground_retry"] == [
        (0, "visible-cache-miss")
    ]


def test_only_the_current_page_gets_foreground_ocr(page) -> None:
    # Two pages side by side, both missing from the cache: the second (not current) one
    # waits for the volume run.
    result = page.run(
        READER + "await openReader(); return __text('#mangaV2PageLabel');",
        before=reader_before({})
        + pages({0: {"cached": MISS, "ocr": READ}, 1: {"cached": MISS, "ocr": READ}}),
        state=READER_SETTINGS,
        localStorage={"pudge.manga.reader.v2": json.dumps({"mode": "double"})},
    )
    assert result.value.startswith("1–2")
    assert reads(result, 0) == ["cached", "ocr"]
    assert reads(result, 1) == ["cached"]


def test_foreground_ocr_is_tried_at_most_once_a_minute_and_only_when_it_can_help(page) -> None:
    failed = {"available": True, "cached": False, "status": "deferred"}
    result = page.run(
        READER
        + r"""
        const turn = async (delta, wait = 300) => { await __click(`[data-manga-v2-action="${delta > 0 ? 'next' : 'previous'}"]`); await __tick(wait); };
        await openReader();                       // page 1: the foreground read finds nothing usable
        await turn(+1); await turn(-1);           // back within a minute: cache only
        await __tick(61000);
        await turn(+1); await turn(-1);           // a minute later: read again
        await turn(+1); await turn(+1); await turn(+1);   // page 3 is verified empty, page 4 has no OCR engine
        return __text('#mangaV2PageLabel');
        """,
        before=reader_before({})
        + pages(
            {
                0: {"cached": MISS, "ocr": failed},
                1: {
                    "cached": {"available": True, "cached": False, "regions": CAT},
                    "ocr": READ,
                },  # text, not yet final
                2: {"cached": {"available": True, "cached": True, "status": "empty_verified"}, "ocr": READ},
                3: {"cached": {"available": False, "cached": False}, "ocr": READ},
            }
        ),
        state=READER_SETTINGS,
    )
    assert result.value == "4 / 4"
    assert reads(result, 0) == ["cached", "ocr", "cached", "cached", "ocr"]
    # A page that already shows text, is verified empty or cannot be read now is left to the volume run.
    assert reads(result, 1) == ["cached"]
    assert reads(result, 2) == ["cached"] and reads(result, 3) == ["cached"]
