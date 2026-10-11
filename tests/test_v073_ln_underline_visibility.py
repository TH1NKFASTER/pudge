"""Light-novel reader, underline mode: words whose Jiten state is selected get a coloured
underline; the rule that clears the other words must not cancel it (behaviour; audit 71Б
rewrite)."""

from __future__ import annotations

from css_support import css_rules
from t71g20_support import STUDY_BOOK, STUDY_VIEW

UNDERLINE = ".ln-reader.word-marks-underline .ln-word"


def test_underline_mode_marks_only_the_selected_states(page) -> None:
    result = page.run(
        STUDY_VIEW + "return {mode: marksMode(), marked: marked()};",
        before=STUDY_BOOK,
        api={
            "light_novel_state": {
                "version": 1,
                "books": [],
                "settings": {"underline_states": ["young", "due"]},
            }
        },
    )
    assert result.value == {"mode": ["word-marks-underline"], "marked": ["戻せば", "目に入った"]}


def test_clearing_rule_leaves_the_marks_their_colour_and_thickness() -> None:
    # Old bug: `text-decoration:none!important` on every word also reset the marked words'
    # underline colour and thickness (the shorthand covers them), so the marks vanished.
    rules = css_rules("index.html")
    plain = rules.declarations(UNDERLINE)
    assert plain == {"color": "inherit!important", "text-decoration-line": "none!important"}
    marked = rules.declarations(UNDERLINE + ".ln-study-mark-enabled")
    assert marked["text-decoration-line"] == "underline!important"
    assert marked["text-decoration-thickness"] == "2px"
    for state in (
        "new",
        "not_in_deck",
        "young",
        "mature",
        "mastered",
        "due",
        "blacklisted",
        "suspended",
        "redundant",
    ):
        colour = rules.get(f"{UNDERLINE}.ln-study-mark-enabled.state-{state}", "text-decoration-color")
        assert colour and colour.startswith(f"var(--pudge-study-{state},"), state
