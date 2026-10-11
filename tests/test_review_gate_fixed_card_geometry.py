"""The review card keeps one geometry: revealing the answer does not move or resize it, and
Show answer / the grade buttons sit on the same bottom line (styles, evaluated per window
size; audit 71Б rewrite). The word staying visible after the reveal is checked in the
window by test_review_gate_undo_and_layout.py."""

import pytest

from study_page_support import cascade, css_length

SHEET = "review_gate.css"
CLOSED = ".pudge-review-gate-front"
REVEALED_WORD = ".pudge-review-gate-card.answer-shown .pudge-review-gate-front"
ANSWER = ".pudge-review-gate-back:not([hidden])"
WINDOWS = [(1300, 600), (1300, 800), (1300, 1200), (500, 600), (500, 900)]


def _variables(width: int) -> dict[str, str]:
    return {k: v for k, v in cascade(SHEET, ".pudge-review-gate-card", width).items() if k.startswith("--")}


def _size(selector: str, width: int, height: int) -> dict[str, float]:
    rules = cascade(SHEET, selector, width)
    return {
        prop: css_length(rules[prop], _variables(width), width=width, height=height)
        for prop in ("height", "min-height", "max-height", "padding-bottom")
        if prop in rules
    }


@pytest.mark.parametrize(("width", "height"), WINDOWS)
def test_closed_and_revealed_states_reserve_the_same_stage_height(width: int, height: int) -> None:
    closed = _size(CLOSED, width, height)
    word = _size(REVEALED_WORD, width, height)
    answer = _size(ANSWER, width, height)
    # Fixed heights: content cannot stretch or shrink either state.
    assert closed["height"] == closed["min-height"]
    assert word["height"] == word["min-height"]
    assert answer["height"] == answer["min-height"] == answer["max-height"]
    # The word slot plus the answer area take exactly the space of the closed card.
    assert word["height"] + answer["height"] == closed["height"]
    assert 280 <= closed["height"] <= 460 and word["height"] == 70
    assert closed["padding-bottom"] == 0
    # The word slot does not shrink in the revealed column.
    assert cascade(SHEET, REVEALED_WORD, width)["flex"].split()[:2] == ["0", "0"]
    # A long answer scrolls inside its area instead of growing the card.
    assert cascade(SHEET, ANSWER, width)["overflow-y"] == "auto"


@pytest.mark.parametrize("width", [1300, 500])
def test_show_answer_and_grade_controls_share_the_bottom_control_line(width: int) -> None:
    def px(value: str) -> float:
        return css_length(value, _variables(width), width=width, height=800)

    control = px("var(--pudge-review-control-height)")
    assert control == (50 if width > 560 else 48)
    show = cascade(SHEET, ".pudge-review-gate-show-answer", width)
    grades = cascade(SHEET, ".pudge-review-gate-back .pudge-review-gate-grades", width)
    buttons = cascade(SHEET, ".pudge-review-gate-grades button", width)
    # Both states are flex columns; margin-top:auto pushes the controls to the bottom.
    assert (
        cascade(SHEET, CLOSED, width)["flex-direction"]
        == cascade(SHEET, ANSWER, width)["flex-direction"]
        == "column"
    )
    assert show["margin-top"] == grades["margin-top"] == "auto"
    for rules in (show, grades, buttons):
        assert px(rules["min-height"]) == control
    for rules in (show, grades):
        grow, shrink, basis = rules["flex"].split()
        assert (grow, shrink, px(basis)) == ("0", "0", control)
    # The grade row stays on the bottom line while a long answer scrolls.
    assert (grades["position"], grades["bottom"]) == ("sticky", "0")
