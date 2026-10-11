"""The manga reader's OCR debug overlay: Shift+D or the toolbar button cycles
Off → Regions → Words; regions are outlined and labelled with where their text and
geometry came from; Words mode draws the real word hitboxes instead of styling the
DOM words (behaviour, page harness + stylesheet; audit 71Б rewrite)."""

from __future__ import annotations

import pytest

from css_support import css_rules
from t71g05_support import READER, READER_SETTINGS, reader_before, vertical_segments

# A narration box found by the dark-block proposal, read by MangaOCR, glyphs from ink.
NARRATION = {
    "text": "迎える",
    "x": 0.6,
    "y": 0.66,
    "width": 0.08,
    "height": 0.24,
    "orientation": "vertical",
    "source": "expanded-vertical-seed",
    "recognizer": "manga-ocr",
    "geometry_source": "ink-grid-v1",
    "segments": vertical_segments("迎える", 0.6, 0.9, 0.08, 0.08, source="ink-grid-v1"),
}


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_shift_d_cycles_the_debug_overlay_and_labels_regions_with_provenance(page, lang) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        const button = '[data-manga-v2-action="toggle-debug-overlay"]';
        const view = () => ({mode: debugMode(), button: __text(button), active: document.querySelector(button)?.classList.contains('active') ?? null,
                             label: document.querySelector('.manga-v2-text-region')?.dataset.debugLabel ?? null,
                             hitboxes: document.querySelectorAll('.manga-v2-debug-token-core').length});
        const out = {toolbar: [...document.querySelectorAll('#mangaReaderV2 .manga-v2-toolbar button')].map(b => `${b.textContent} ${b.title}`)};
        await __key('D', {shiftKey: true}); await __tick(20);
        out.regions = view();
        await __key('D', {shiftKey: true}); await __tick(20);
        out.words = view();
        await __key('D', {shiftKey: true}); await __tick(20);
        out.off = view();
        // Not a shortcut: D alone, Cmd+Shift+D, Shift+D typed into a field.
        await __key('D'); await __key('D', {shiftKey: true, metaKey: true});
        const field = document.createElement('input'); document.getElementById('mangaReaderV2').appendChild(field);
        await __key('D', {shiftKey: true}, 'input'); await __tick(20);
        out.ignored = debugMode();
        await __click(button); await __tick(20);
        out.clicked = view();
        return {out, texts: ['manga.ocrDebugOff', 'manga.ocrDebugRegions', 'manga.ocrDebugWords'].map(key => PudgeI18n.t(key))};
        """,
        lang=lang,
        before=reader_before({0: [NARRATION]}, {"迎える": ["迎え", "る"]}),
        state=READER_SETTINGS,
    )
    out, (off, regions, words) = result.value["out"], result.value["texts"]
    label = "R0 · manga-ocr/expanded-vertical-seed/ink-grid-v1 · 迎え | る"
    assert out["regions"] == {
        "mode": "regions",
        "button": regions,
        "active": True,
        "label": label,
        "hitboxes": 0,
    }
    assert out["words"] == {"mode": "words", "button": words, "active": True, "label": label, "hitboxes": 2}
    assert out["off"] == {"mode": "off", "button": off, "active": False, "label": label, "hitboxes": 0}
    assert out["ignored"] == "off"
    assert out["clicked"]["mode"] == "regions"
    # The visible page is read automatically: there is no per-page "Recognize" action.
    assert not [text for text in out["toolbar"] if "Recognize" in text or "Распознать" in text]


def test_debug_overlay_follows_the_reader_from_opening_and_across_pages(page) -> None:
    other = {
        "text": "猫だ",
        "x": 0.3,
        "y": 0.74,
        "width": 0.08,
        "height": 0.16,
        "orientation": "vertical",
        "source": "vision",
        "segments": vertical_segments("猫だ", 0.3, 0.9, 0.08, 0.08),
    }
    result = page.run(
        READER
        + r"""
        await openReader();
        const out = {button: document.querySelector('[data-manga-v2-action="toggle-debug-overlay"]')};
        await setDebug('words');
        await __click('[data-manga-v2-action="next"]'); await __tick(300);
        out.page = __text('#mangaV2PageLabel');
        out.labels = [...document.querySelectorAll('.manga-v2-text-region')].map(n => n.dataset.debugLabel ?? null);
        out.hitboxes = [...document.querySelectorAll('.manga-v2-debug-token-core')].map(n => n.dataset.word);
        return out;
        """,
        before=reader_before({0: [NARRATION], 1: [other]}, {"迎える": ["迎え", "る"], "猫だ": ["猫", "だ"]}),
        state=READER_SETTINGS,
    )
    out = result.value
    # The toolbar button is a debugging aid: not offered until Shift+D is pressed.
    assert out["button"] is None
    # After a page turn in Words mode the new page is labelled and shows its hitboxes
    # (they stay empty: nothing re-syncs the overlay when the text layer changes).
    assert out["page"] == "2 / 4"
    assert out["labels"] == ["R0 · vision · 猫 | だ"]
    assert out["hitboxes"] == ["猫", "だ"]


def test_debug_overlay_styles() -> None:
    rules = css_rules("manga_reader_v2.css")
    reader = '.manga-v2-reader[data-debug-overlay="{}"]'
    for mode in ("regions", "words"):
        # Regions are outlined and show their label above them.
        assert rules.get(f"{reader.format(mode)} .manga-v2-text-region", "outline")
        assert (
            rules.get(f"{reader.format(mode)} .manga-v2-text-region::before", "content")
            == "attr(data-debug-label)"
        )
    assert rules.get(".manga-v2-text-region::before", "content") is None
    # Words mode: the DOM words (laid out as text, not on the glyphs) stay invisible;
    # the drawn hitbox (solid) and its click forgiveness (dashed) show what a click hits.
    word = f"{reader.format('words')} .manga-v2-region-content .pudge-study-word"
    assert rules.get(word, "outline") == "none!important"
    assert rules.get(word, "background") == "transparent!important"
    assert rules.get(f"{reader.format('words')} .manga-v2-region-content", "color") == "transparent!important"
    assert "solid" in rules.get(".manga-v2-debug-token-core", "border")
    assert "dashed" in rules.get(".manga-v2-debug-token-slop", "border")
    assert rules.get(".manga-v2-debug-hitbox-layer", "pointer-events") == "none"
