"""Alternate readings shown by the review gate and the sidebar review card (one shared
line, PudgeJitenWords.readingsLine), and the shared card prefetch (behaviour in the window;
audit 71Б rewrite)."""

import json

from study_page_support import GATE, GATE_VIEW

# 生 (reading なま, index 1) also reads せい / しょう; the selected reading is not repeated.
SEI_BRACKETS = {
    "wordTextPlain": "生",
    "readingIndex": 1,
    "readings": [
        {"text": "生[せい]", "readingIndex": 0},
        {"text": "生[なま]", "readingIndex": 1},
        {"text": " セイ "},
        {"text": "生[しょう]"},
    ],
}
SEI_RUBY = {
    "wordId": 50,
    "wordTextPlain": "生",
    "readingIndex": 1,
    "definitions": [{"meanings": ["raw"]}],
    "readings": [
        {"text": "生", "rubyText": "生[せい]", "readingIndex": 0},
        {"text": "生", "rubyText": "生[なま]", "readingIndex": 1},
        {"text": "生", "rubyText": "生[しょう]", "readingIndex": 2},
    ],
}
LINES = {
    "kana_only": {"wordTextPlain": "やらかす", "reading": "やらかす", "readings": [{"text": "やらかす"}]},
    "single_bracket": {
        "wordTextPlain": "伏せる",
        "readingIndex": 0,
        "readings": [{"text": "伏[ふ]せる", "readingIndex": 0}],
    },
    "alternates_in_text": SEI_BRACKETS,
    "alternates_in_ruby": SEI_RUBY,
}


def test_reading_line_shows_only_other_pronunciations(page) -> None:
    result = page.run(f"""
        const cards = {json.dumps(LINES, ensure_ascii=False)};
        return Object.fromEntries(Object.entries(cards).map(([name, card]) => [name, PudgeJitenWords.readingsLine(card)]));
        """)
    assert result.value == {
        "kana_only": "",  # a kana word has no extra reading line
        "single_bracket": "",  # 伏[ふ]せる is the word itself, not another reading
        "alternates_in_text": "せい ・ しょう",
        "alternates_in_ruby": "せい ・ しょう",
    }


def test_review_gate_and_sidebar_card_show_the_same_reading_line(page) -> None:
    result = page.run(
        GATE_VIEW
        + """
        await __tick(2000);                                          // the sidebar loads its due card
        await __click('[data-sc-reveal]'); await __tick(10);
        const sidebar = __text('.sidebar-due-readings');
        await startPlay(EPISODE); await __tick(20); await space();
        return {sidebar, gate: __text('.pudge-review-gate-reading')};
        """,
        before=GATE
        + f"""
        window.GATE_CARDS = [{json.dumps({**SEI_BRACKETS, "wordId": 51, "definitions": [{"meanings": ["raw"]}]}, ensure_ascii=False)}];
        __api.sidebar_due_review_cards = async () => ({{ok: true, configured: true, cards: window.GATE_CARDS}});
        """,
    )
    assert result.value == {"sidebar": "せい ・ しょう", "gate": "せい ・ しょう"}


def test_shared_prefetch_moves_eight_ahead_and_is_account_scoped(page) -> None:
    # The current card and eight ahead are warmed (study_word) once per account.
    result = page.run(
        """
        window.SCOPE = 'a';
        __api.study_provider_capabilities = async () => ({account_key: window.SCOPE});
        __api.study_word = async card => card;
        __apiCalls.length = 0;
        const cards = Array.from({length: 12}, (_, i) => ({wordId: i + 1, readingIndex: 0}));
        const warmed = () => __apiCalls.filter(c => c.name === 'study_word').map(c => `${c.args[0].wordId}`);
        await PudgeJitenWords.prefetch(cards); await __flush();
        const first = warmed();
        await PudgeJitenWords.prefetch(cards.slice(1)); await __flush();
        const next = warmed();
        await PudgeJitenWords.prefetch(cards.slice(1)); await __flush();
        const again = warmed();
        window.SCOPE = 'b';
        await PudgeJitenWords.prefetch(cards); await __flush();
        return {first, next, again, other: warmed().slice(again.length)};
        """
    )
    nine = [str(i) for i in range(1, 10)]
    assert result.value["first"] == nine
    assert result.value["next"] == [*nine, "10"]
    assert result.value["again"] == result.value["next"]
    assert result.value["other"] == nine  # another account warms its own cards
