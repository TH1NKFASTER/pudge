"""Manga OCR text lives only in the bubble overlay: the text sits invisibly in place over the
bubble (selectable, no white card over the artwork, no hover card), and the old fixed OCR
panel and its whole-page OCR API are gone (behaviour, page harness; audit 71Б rewrite)."""

from __future__ import annotations

from css_support import css_rules
from pudge.manga import MangaService
from pudge.web_app import WebAppApi
from tests.t71g06_support import column, reader
from tests.t71g07_support import CLICKS

BUBBLE = column("猫が好き", 0.7, 0.8, source="vision-character-box", status=None)
WORDS = {"猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]]}
# The parse of the bubble waits for window.__release().
HELD_PARSE = r"""
const parseText = __api.study_parse_text;
__api.study_parse_text = async text => { await new Promise(resolve => { window.__release = resolve; }); return parseText(text); };
"""


def test_ocr_text_sits_invisibly_in_place_over_the_bubble(page) -> None:
    result = reader(
        page,
        CLICKS
        + r"""
        const region = regionNode(0), content = () => region.querySelector('.manga-v2-region-content');
        const copy = region.querySelector('.manga-v2-selection-content');
        const out = {beforeParse: {text: content().textContent, words: content().querySelectorAll('[data-pudge-study-token]').length},
                     copy: copy.textContent, title: copy.title, expectedTitle: t('manga.selectOcrTextTripleClick')};
        window.__release(); await __tick(50);
        out.words = [...content().querySelectorAll('[data-pudge-study-token]')].map(n => n.textContent);
        // Resting the pointer on a word opens no card (only a click does).
        await __fire('#mangaV2Pages [data-pudge-study-token]', 'pointerover', {}, 'PointerEvent'); await __tick(1500);
        out.hoverCard = card();
        out.click = await clickImage(.725, .763);
        return out;
        """,
        regions={0: [BUBBLE]},
        parse=WORDS,
        before=HELD_PARSE,
    )
    out = result.value
    # The OCR text is in the bubble at once, as a selectable copy too, then becomes words.
    assert out["beforeParse"] == {"text": "猫が好き", "words": 0}
    assert out["copy"] == "猫が好き" and out["title"] == out["expectedTitle"]
    assert out["words"] == ["猫", "が", "好き"]
    assert out["hoverCard"] is None and out["click"] == "猫"

    rules = css_rules("manga_reader_v2.css")
    # No card over the artwork: the overlay has no background or shadow, and its text is
    # invisible (the picture shows through) ...
    for selector in (".manga-v2-text-region", ".manga-v2-region-content", ".manga-v2-selection-content"):
        assert rules.get(selector, "background") == "transparent", selector
    assert rules.get(".manga-v2-region-content", "box-shadow") == "none"
    for selector in (".manga-v2-region-content", ".manga-v2-region-content *"):
        assert rules.get(selector, "-webkit-text-fill-color") == "transparent!important", selector
    assert rules.get(".manga-v2-selection-content", "-webkit-text-fill-color") == "transparent"
    # ... while a selection of it is visible.
    assert "rgba(94,151,255" in rules.get(".manga-v2-selection-content::selection", "background")
    # The bubble and its words take neither the mouse nor the selection; the copy does.
    for selector in (".manga-v2-text-region", ".manga-v2-region-content"):
        assert (rules.get(selector, "pointer-events"), rules.get(selector, "user-select")) == (
            "none",
            "none",
        ), selector
    copy = rules.declarations(".manga-v2-selection-content")
    assert (copy["pointer-events"], copy["user-select"]) == ("auto", "text")


def test_legacy_fixed_ocr_panel_and_page_ocr_api_are_gone(page) -> None:
    # The window API and the manga service offer no whole-page OCR any more.
    for name in ("manga_ocr_page", "manga_ocr_cached_page"):
        assert not hasattr(WebAppApi, name), name
    for name in ("ocr_page", "cached_ocr_page"):
        assert not hasattr(MangaService, name), name
    # Reading a volume and asking for OCR (the O key) uses the region overlay only.
    result = reader(
        page,
        r"""
        await __key('o'); await __tick(200);
        return {legacy: document.querySelectorAll('.manga-reader, .manga-ocr-text, #mangaOcrText, #mangaReader').length,
                overlay: regions().map(r => r.text)};
        """,
        regions={0: [BUBBLE]},
        parse=WORDS,
    )
    assert result.value == {"legacy": 0, "overlay": ["猫が好き"]}
    # O reads the page's regions again (refresh, not only the cache).
    assert result.calls_of("manga_text_regions")[-1] == [3, 0, True, False]
    assert not {call["name"] for call in result.calls} & {"manga_ocr_page", "manga_ocr_cached_page"}
