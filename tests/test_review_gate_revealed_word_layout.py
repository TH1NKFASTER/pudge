"""After the reveal the question word stays at its place, with furigana and the pitch accent
right below it (styles and the window; audit 71Б rewrite)."""

import pytest

from study_page_support import GATE, GATE_VIEW, cascade, css_length

SHEET = "review_gate.css"
CLOSED = ".pudge-review-gate-front"
REVEALED = ".pudge-review-gate-card.answer-shown .pudge-review-gate-front"


def _padding(rules: dict[str, str]) -> dict[str, str]:
    parts = rules["padding"].split()
    top, right, bottom = {1: parts * 3, 2: [parts[0], parts[1], parts[0]]}.get(len(parts), parts[:3])
    sides = {"top": top, "right": right, "bottom": bottom}
    for side in sides:
        sides[side] = rules.get(f"padding-{side}", sides[side])
    return sides


@pytest.mark.parametrize("width", [1300, 500])
def test_revealed_word_slot_collapses_without_moving_the_word(width: int) -> None:
    closed, revealed = cascade(SHEET, CLOSED, width), cascade(SHEET, REVEALED, width)
    variables = {
        k: v for k, v in cascade(SHEET, ".pudge-review-gate-card", width).items() if k.startswith("--")
    }
    # The word keeps its top position: same top padding, content starts at the top.
    assert _padding(closed)["top"] == _padding(revealed)["top"] == "18px"
    assert closed["justify-content"] == "flex-start"
    # The emptied Show answer area collapses: only the word line is left.
    assert css_length(revealed["min-height"], variables, width=width, height=800) == 70
    assert css_length(_padding(revealed)["bottom"], variables, width=width, height=800) == 0
    assert revealed["gap"] == "0"
    assert (
        cascade(SHEET, ".pudge-review-gate-card.answer-shown .pudge-review-gate-back", width)["padding-top"]
        == "0"
    )


def test_revealed_ruby_and_pitch_are_visually_grouped(page) -> None:
    ruby = cascade(SHEET, ".pudge-review-gate-front .pudge-review-gate-word ruby")
    rt = cascade(SHEET, ".pudge-review-gate-front .pudge-review-gate-word rt")
    pitch = cascade(SHEET, ".pudge-review-gate-card.answer-shown .pudge-review-gate-pitch")
    assert ruby["ruby-align"] == "center"
    assert (rt["line-height"], rt["letter-spacing"]) == ("1", "0")
    assert cascade(SHEET, ".pudge-review-gate-pitch")["display"] == "flex"
    assert (pitch["justify-content"], pitch["margin-top"]) == ("center", "0")
    # In the window: the pitch accent of the word appears with the answer.
    result = page.run(
        GATE_VIEW
        + """
        await startPlay(EPISODE); await __tick(20);
        const before = shown('.pudge-review-gate-pitch');
        await space();
        return {before, after: shown('.pudge-review-gate-pitch'), text: __text('.pudge-review-gate-pitch')};
        """,
        before=GATE + "window.GATE_CARDS[0].pitchAccents = [1];",
    )
    assert result.value["before"] is False and result.value["after"] is True
    assert result.value["text"].replace("\n", "") == "ねこ"
