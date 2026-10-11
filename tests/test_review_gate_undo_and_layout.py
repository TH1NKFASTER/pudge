from __future__ import annotations

import json
import pudge.review_providers as review_providers
from css_support import css_rules
from pudge.review_gate import EpisodeReviewIdentity, ReviewGateStore
from study_page_support import CARD, GATE, GATE_VIEW


class _State:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_state(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.values[key] = value


class _Response:
    status_code = 200
    content = b'{"success":true,"cardDeleted":false}'

    def json(self):
        return json.loads(self.content)


def test_review_gate_store_can_unconfirm_after_provider_undo() -> None:
    state = _State()
    store = ReviewGateStore(state, prefix="undo-test")
    identity = EpisodeReviewIdentity(10, 2)
    store.mark_confirmed("account", identity, required=2, card_key="1:0")
    second = store.mark_confirmed("account", identity, required=2, card_key="2:0")
    assert second.granted
    undone = store.unmark_confirmed("account", identity, required=2, card_key="2:0")
    assert undone.confirmed_card_keys == ("1:0",)
    assert undone.completed == 1
    assert undone.remaining == 1
    assert not undone.granted


def test_jiten_provider_uses_native_undo_endpoint(monkeypatch) -> None:
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr(review_providers.httpx, "post", fake_post)
    result = review_providers.JitenReviewProvider("token").undo_review(123, 4)
    assert result["outcome"] == "undone"
    assert calls[0][0].endswith("/api/srs/undo-review")
    assert calls[0][1]["json"] == {"wordId": 123, "readingIndex": 4}


def test_revealing_the_answer_keeps_the_word_in_its_slot_and_adds_the_reading(page) -> None:
    # One stable word slot: the question word stays where it was and gets its furigana;
    # the answer side does not repeat the word below it.
    for lang in ("en", "ru"):
        result = page.run(
            GATE_VIEW
            + r"""
            await startPlay(EPISODE); await __tick(20);
            const slot = () => ({text: frontWord().innerText, ruby: frontWord().querySelector('rt')?.textContent ?? null,
                                 shown: shown('.pudge-review-gate-front .pudge-review-gate-word')});
            const out = {closed: {...slot(), answer: shown('.pudge-review-gate-back'), button: shown('[data-review-gate-show-answer]'),
                                  status: __text('.pudge-review-gate-status')}};
            await space();
            out.revealed = {...slot(), answer: shown('.pudge-review-gate-back'), button: shown('[data-review-gate-show-answer]'),
                            repeated: shown('.pudge-review-gate-back .pudge-review-gate-answer-word'),
                            meanings: __text('.pudge-review-gate-meanings'),
                            grades: [...gate().querySelectorAll('[data-review-gate-grade]')].map(b => [b.dataset.reviewGateGrade, b.innerText])};
            await __click('.pudge-review-gate-front [data-review-gate-jiten]');
            out.expected = {status: PudgeI18n.t('gate.reviewedOf', {completed: 0, required: 2}),
                            grades: ['again', 'hard', 'good', 'easy'].map(id => t(`review.action.${id}`))};
            return out;
            """,
            lang=lang,
            before=GATE,
        )
        out = result.value
        assert out["closed"] == {
            "text": "猫",
            "ruby": None,
            "shown": True,
            "answer": False,
            "button": True,
            "status": out["expected"]["status"],
        }
        assert out["revealed"]["ruby"] == "ねこ" and out["revealed"]["text"].replace("\n", "") == "猫ねこ"
        assert out["revealed"]["shown"] is True and out["revealed"]["answer"] is True
        assert out["revealed"]["button"] is False and out["revealed"]["repeated"] is False
        assert out["revealed"]["meanings"] == "meaning 1"
        assert [grade for grade, _label in out["revealed"]["grades"]] == ["again", "hard", "good", "easy"]
        assert [label.split("\n")[0] for _grade, label in out["revealed"]["grades"]] == out["expected"][
            "grades"
        ]
        # The word links to its Jiten page.
        assert result.calls_of("open_url") == [["https://jiten.moe/vocabulary/1/0"]]
    # The word is set in the Mincho face of the answer, also before the reveal.
    assert (
        css_rules("review_gate.css").get(".pudge-review-gate-front .pudge-review-gate-word", "font-family")
        == '"Hiragino Mincho ProN","Yu Mincho",serif'
    )


GRADE_GOOD = "await space(); await gradeKey(3); await space(); await __tick(10);"


def test_previous_review_strip_undoes_with_the_button_and_command_z(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            GATE_VIEW
            + f"""
            const strip = () => __text('.pudge-review-gate-previous');
            await startPlay(EPISODE); await __tick(20);
            {GRADE_GOOD}
            const out = {{graded: {{strip: strip(), word: frontWord().innerText, status: __text('.pudge-review-gate-status')}}}};
            await __key('z', {{code: 'KeyZ', metaKey: true}}, 'body'); await __tick(10);
            out.commandZ = {{strip: strip(), word: frontWord().innerText, status: __text('.pudge-review-gate-status'),
                            answer: shown('.pudge-review-gate-back'), enabled: !gate().querySelector('[data-review-gate-grade]')?.disabled}};
            {GRADE_GOOD}
            await __click('[data-review-gate-undo]'); await __tick(10);
            out.button = {{strip: strip(), word: frontWord().innerText}};
            await __key('z', {{code: 'KeyZ', ctrlKey: true}}, 'body'); await __tick(10);   // only ⌘Z undoes
            out.expected = {{strip: [PudgeI18n.t('gate.previous'), '猫', t('review.action.good'), PudgeI18n.t('manga.undo')].join('\\n'),
                            first: PudgeI18n.t('gate.reviewedOf', {{completed: 1, required: 2}}),
                            none: PudgeI18n.t('gate.reviewedOf', {{completed: 0, required: 2}})}};
            return out;
            """,
            lang=lang,
            before=GATE,
        )
        out = result.value
        assert out["graded"] == {
            "strip": out["expected"]["strip"],
            "word": "犬",
            "status": out["expected"]["first"],
        }
        assert out["commandZ"] == {
            "strip": None,
            "word": "猫",
            "status": out["expected"]["none"],
            "answer": False,
            "enabled": True,
        }
        assert out["button"] == {"strip": None, "word": "猫"}
        assert [call[1:4] for call in result.calls_of("review_gate_review")] == [
            [1, 0, "good"],
            [1, 0, "good"],
        ]
        assert result.calls_of("review_gate_undo") == [
            ["/lib/Show - 03.mkv", 1, 0],
            ["/lib/Show - 03.mkv", 1, 0],
        ]


def test_reading_gate_undo_goes_to_the_content_review(page) -> None:
    # The gate before a light-novel chapter (content review) undoes through its own call.
    result = page.run(
        GATE_VIEW
        + CARD
        + GRADE_GOOD.join(
            [
                """
        __api.content_review_begin = async () => ({blocking: true, granted: false, required: 2, completed: 0, remaining: 2, token: 'tok-1',
                                                   cards: [reviewCard(5, '本', 'ほん'), reviewCard(6, '紙', 'かみ')]});
        __api.content_review_review = async () => ({ok: true, outcome: 'confirmed', blocking: true, granted: false, required: 2, completed: 1, remaining: 1});
        __api.content_review_undo = async () => ({ok: true, outcome: 'undone', blocking: true, granted: false, required: 2, completed: 0, remaining: 2});
        void PudgeReviewGate.require('ln', 6, 0); await __tick(10);
        """,
                """
        await __key('z', {code: 'KeyZ', metaKey: true}, 'body'); await __tick(10);
        return frontWord().innerText;
        """,
            ]
        ),
    )
    assert result.value == "本"
    assert [call[:4] for call in result.calls_of("content_review_review")] == [["tok-1", 5, 0, "good"]]
    assert result.calls_of("content_review_undo") == [["tok-1", 5, 0]]
    assert not result.called("review_gate_undo")


def test_undo_shows_the_previous_card_at_once_and_waits_for_the_review_to_land(page) -> None:
    # A slow network: the review is still on its way when the user presses ⌘Z. The card is
    # back on screen immediately; the undo is sent only after the review was confirmed.
    result = page.run(
        GATE_VIEW
        + """
        await startPlay(EPISODE); await __tick(20);
        window.HOLD_REVIEW = true;
        """
        + GRADE_GOOD
        + """
        await __key('z', {code: 'KeyZ', metaKey: true}, 'body');
        const out = {word: frontWord().innerText, undoSent: __apiCalls.some(c => c.name === 'review_gate_undo'),
                     gradesDisabled: [...gate().querySelectorAll('[data-review-gate-grade]')].every(b => b.disabled)};
        await space();                                        // reveal while the undo is on its way
        out.gradesWhileUndoing = [...gate().querySelectorAll('[data-review-gate-grade]')].every(b => b.disabled);
        window.HOLD_REVIEW = false; window.releaseReview(); await __tick(10);
        out.after = {word: frontWord().innerText, undoSent: __apiCalls.some(c => c.name === 'review_gate_undo'),
                     enabled: [...gate().querySelectorAll('[data-review-gate-grade]')].every(b => !b.disabled)};
        return out;
        """,
        before=GATE,
    )
    out = result.value
    assert out["word"] == "猫" and out["undoSent"] is False
    assert out["gradesWhileUndoing"] is True
    assert out["after"] == {"word": "猫ねこ", "undoSent": True, "enabled": True}
    assert [
        name
        for name in (call["name"] for call in result.calls)
        if name in ("review_gate_review", "review_gate_undo")
    ] == ["review_gate_review", "review_gate_undo"]
