"""Escape closes the word card even with leftover selections; jpdb's five grades fit
(behaviour in the window; audit 71Б rewrite)."""

from __future__ import annotations

from css_support import css_rules
from study_page_support import LN_BOOK, SELECTION

LN_BOOKS = {
    "version": 1,
    "settings": {},
    "books": [{"id": 6, "title": "Novel", "volume": 1}, {"id": 7, "title": "Novel", "volume": 2}],
}
READER = {
    "before": LN_BOOK,
    "state": {"settings": {"review_gate_ln_enabled": False}},
    "api": {"light_novel_state": LN_BOOKS},
}

# Two volumes stay selected on the shelf, a word card is open in the reader, and the
# reader's first paragraph is selected as text.
OPEN_CARD = (
    SELECTION
    + """
setPage('lightnovels'); await __tick(50);
toggleLnSelection(6); toggleLnSelection(7);
await __settle(openLightNovel(6)); await __tick(300);
await __click('#lnReader [data-ln-token="0:0"]'); await __tick(20);
select(document.querySelector('#lnReader [data-ln-paragraph="1"]'));
const card = document.getElementById('pudgeStudyCard');
const escape = type => __fire('body', type, {key: 'Escape', code: 'Escape'}, 'KeyboardEvent');
const state = () => ({card: card.classList.contains('open'), library: ui.lnSelection.size,
                      text: !getSelection().isCollapsed, reader: $('lnReaderShell').classList.contains('open')});
const before = state();
"""
)


def test_escape_closes_word_card_first_despite_leftover_selections(page) -> None:
    # The card used to stay open: a leftover selection swallowed the Escape (only the close
    # button's focus ring appeared).
    result = page.run(
        OPEN_CARD + "await escape('keydown'); await __tick(10); return {before, after: state()};", **READER
    )
    assert result.value["before"] == {"card": True, "library": 2, "text": True, "reader": True}
    assert result.value["after"] == {"card": False, "library": 2, "text": True, "reader": True}


def test_keyup_fallback_closes_word_card_when_webkit_ate_keydown(page) -> None:
    result = page.run(OPEN_CARD + "await escape('keyup'); await __tick(10); return state();", **READER)
    assert result.value == {"card": False, "library": 2, "text": True, "reader": True}
    assert result.calls_of("log_ui_event")[-1] == ["escape.study_keyup_fallback", {"keydown_seen": False}]


def test_one_escape_press_closes_only_the_word_card(page) -> None:
    result = page.run(
        OPEN_CARD
        + """
        await escape('keydown'); await escape('keyup'); await __tick(10);
        const first = state();
        await escape('keydown'); await escape('keyup'); await __tick(10);
        return {first, second: state()};
        """,
        **READER,
    )
    assert result.value["first"] == {"card": False, "library": 2, "text": True, "reader": True}
    assert result.value["second"]["library"] == 0 and result.value["second"]["reader"] is True


def test_escape_closes_context_menu_before_search_overlay_exists(page) -> None:
    """Regression: the lazily created search overlay counted as "open" while absent."""
    result = page.run(
        """
        await __fire('#currentContent [data-media-id="7"]', 'contextmenu', {clientX: 50, clientY: 60}, 'MouseEvent');
        const out = {overlay: Boolean(document.getElementById('globalSearchOverlay')), menu: $('contextMenu').classList.contains('open')};
        await __key('Escape', {code: 'Escape'}, 'body'); await __tick(10);
        out.closed = !$('contextMenu').classList.contains('open');
        out.fullscreenAfterMenu = __apiCalls.filter(c => c.name === 'exit_fullscreen').length;
        await __key('Escape', {code: 'Escape'}, 'body'); await __tick(10);    // nothing left open: leave full screen
        out.fullscreenAfterNext = __apiCalls.filter(c => c.name === 'exit_fullscreen').length;
        return out;
        """,
        state={
            "home": {
                "waiting": [
                    {
                        "media_id": 7,
                        "title": "Show",
                        "next_episode": 3,
                        "site_url": "https://anilist.co/anime/7",
                    }
                ]
            }
        },
    )
    assert result.value == {
        "overlay": False,
        "menu": True,
        "closed": True,
        "fullscreenAfterMenu": 0,
        "fullscreenAfterNext": 1,
    }


def test_five_grade_row_gets_its_own_line_and_labels_cannot_overlap(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            await __settle(openLightNovel(6)); await __tick(300);
            await __click('#lnReader [data-ln-token="0:0"]'); await __tick(20);
            const row = document.querySelector('#pudgeStudyCard .pudge-study-action-row');
            return {count: row.dataset.count, grades: [...row.querySelectorAll('[data-pudge-study-review]')].map(b => b.textContent),
                    expected: ['nothing', 'something', 'hard', 'okay', 'easy'].map(id => t(`review.action.${id}`))};
            """,
            lang=lang,
            before=LN_BOOK,
            state={"settings": {"review_gate_ln_enabled": False}},
            api={"light_novel_state": {"version": 1, "settings": {"study_backend": "jpdb"}, "books": []}},
        )
        assert result.value["count"] == "5"
        assert result.value["grades"] == result.value["expected"]
    rules = css_rules("reading_tools.css")
    # Five grades take the whole line; the Add button moves to the next line, right-aligned.
    assert rules.get('.pudge-study-action-row[data-count="5"]', "flex-wrap") == "wrap"
    assert (
        rules.get('.pudge-study-action-row[data-count="5"]>.pudge-study-review-actions', "flex") == "1 1 100%"
    )
    add = rules.declarations('.pudge-study-action-row[data-count="5"]>.pudge-study-add-wrap')
    assert (add["flex"], add["justify-content"], add["padding-left"], add["border-left"]) == (
        "1 1 100%",
        "flex-end",
        "0",
        "0",
    )
    assert (
        rules.get('.pudge-study-review-actions[data-count="5"]', "grid-template-columns")
        == "repeat(5,minmax(0,1fr))"
    )
    # A long label is cut with an ellipsis instead of running into its neighbour.
    grade = rules.declarations(".pudge-study-review-actions .pudge-study-grade")
    assert (grade["min-width"], grade["white-space"], grade["overflow"], grade["text-overflow"]) == (
        "0",
        "nowrap",
        "hidden",
        "ellipsis",
    )


def test_grade_key_settings_are_collapsible(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('settings'); await __tick(20);
            const keys = () => [...document.querySelectorAll('#s_review_shortcuts_group .shortcut-recorder[data-review-binding]')];
            const out = {summary: __text('#s_review_shortcuts_group > summary'), expected: PudgeI18n.t('ui.gradeKeys'),
                         count: keys().length, group: __visible('#s_review_shortcuts_group > summary'), shownClosed: keys().some(el => el.checkVisibility())};
            $('s_review_shortcuts_group').open = true;      // what a click on the summary does (dom_lite has no default action for it)
            out.shownOpen = keys().every(el => el.checkVisibility());
            return out;
            """,
            lang=lang,
        )
        out = result.value
        assert out["summary"] == out["expected"]
        assert (
            out["count"] >= 4
            and out["group"] is True
            and out["shownClosed"] is False
            and out["shownOpen"] is True
        )
