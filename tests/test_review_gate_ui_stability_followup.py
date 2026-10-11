"""Review gate and word card stay visually stable: a loading gate stays opaque and can be
closed, grade buttons create no hover effects, and no focus is left on a button that goes
away (behaviour and styles; audit 71Б rewrite)."""

from css_support import css_rules
from study_page_support import GATE, GATE_VIEW, LN_BOOK, cascade


def test_loading_gate_is_not_made_translucent_and_can_be_closed(page) -> None:
    # While the cards are authorized the overlay shows a progress cursor; it is neither
    # see-through nor click-through, and closing it still works (no episode starts).
    result = page.run(
        GATE_VIEW
        + """
        __api.review_gate_begin = () => new Promise(() => {});            // still authorizing
        void startPlay(EPISODE); await __tick(20);
        const out = {open: PudgeReviewGate.isOpen(), busy: gate().classList.contains('pudge-review-gate-busy'),
                     loading: __text('.pudge-review-gate-loading'), expected: PudgeI18n.t('gate.loadingJitenReviewCard')};
        await __click('[data-review-gate-close]'); await __tick(20);
        out.closed = !PudgeReviewGate.isOpen() && !__visible('#pudgeReviewGate');
        return out;
        """,
        before=GATE,
    )
    out = result.value
    assert out["open"] is True and out["busy"] is True and out["loading"] == out["expected"]
    assert out["closed"] is True
    assert not result.called("play")
    busy = cascade("review_gate.css", ".pudge-review-gate-busy")
    assert busy == {"cursor": "progress"}
    for _media, _selector, declarations in css_rules("review_gate.css").rules:
        assert "backdrop-filter" not in declarations


def test_grade_hover_does_not_create_extra_compositor_effects() -> None:
    assert cascade("review_gate.css", ".pudge-review-gate-card")["isolation"] == "isolate"
    for sheet, selector in (
        ("review_gate.css", ".pudge-review-gate-grades button"),
        ("reading_tools.css", ".pudge-study-grade"),
    ):
        for state in ("", ":hover", ":active"):
            rules = css_rules(sheet).declarations(selector + state)
            for prop in ("transform", "filter", "transition", "box-shadow"):
                assert rules.get(prop, "").replace(" ", "") == "none!important", (selector + state, prop)


# Records focus leaving an element that is still in the page (a blur), per element.
BLURS = """
window.BLURRED = [];
document.addEventListener('focusout', event => { if (event.target.isConnected) window.BLURRED.push(event.target.dataset.reviewGateGrade || event.target.className); }, true);
"""


def test_review_gate_blurs_the_focused_button_before_replacing_it(page) -> None:
    # WebKit kept the hover/focus look of a button removed by innerHTML ("ghost" buttons).
    result = page.run(
        GATE_VIEW
        + BLURS
        + """
        await startPlay(EPISODE); await __tick(20);
        gate().querySelector('[data-review-gate-show-answer]').focus();
        await space();                                                     // re-render with the answer
        const out = {afterReveal: window.BLURRED.slice(), focused: document.activeElement?.dataset.reviewGateGrade};
        window.BLURRED.length = 0;
        await space();                                                     // confirm Good: the next card replaces it
        await __tick(10);
        out.afterGrade = window.BLURRED.slice();
        out.active = document.activeElement === document.body;
        return out;
        """,
        before=GATE,
    )
    out = result.value
    assert out["afterReveal"] == ["primary pudge-review-gate-show-answer"]
    assert out["focused"] == "good"
    assert out["afterGrade"][:1] == ["good"]
    assert out["active"] is True


def test_single_bracket_ruby_reading_is_not_rendered_twice(page) -> None:
    # Jiten sends 伏[ふ]せる as the only reading: the answer shows it once, as furigana.
    result = page.run(
        GATE_VIEW
        + """
        await startPlay(EPISODE); await __tick(20);
        await space();
        return {word: frontWord().innerText.replace(/\\n/g, ''), ruby: frontWord().querySelector('rt')?.textContent,
                readingLine: shown('.pudge-review-gate-reading')};
        """,
        before=GATE
        + """
        window.GATE_CARDS = [{wordId: 7, readingIndex: 0, wordTextPlain: '伏せる', readings: [{text: '伏[ふ]せる', readingIndex: 0}],
                              definitions: [{meanings: ['to lie face down']}]}];
        """,
    )
    assert result.value == {"word": "伏ふせる", "ruby": "ふ", "readingLine": False}


def test_closing_the_word_card_does_not_leave_focus_on_its_buttons(page) -> None:
    result = page.run(
        BLURS
        + """
        await __settle(openLightNovel(6)); await __tick(300);
        await __click('#lnReader [data-ln-token="0:0"]'); await __tick(20);
        const card = document.getElementById('pudgeStudyCard');
        await __key('3', {code: 'Digit3'}, 'body');                        // the grade key selects Good
        const out = {open: card.classList.contains('open'), focused: document.activeElement?.dataset.pudgeStudyReview};
        await __key('Escape', {code: 'Escape'}, 'body'); await __tick(10);
        out.closed = !card.classList.contains('open');
        out.blurred = window.BLURRED.filter(name => String(name).includes('pudge-study-grade')).length;
        out.focusInCard = card.contains(document.activeElement);
        return out;
        """,
        before=LN_BOOK,
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    assert result.value == {
        "open": True,
        "focused": "good",
        "closed": True,
        "blurred": 1,
        "focusInCard": False,
    }
