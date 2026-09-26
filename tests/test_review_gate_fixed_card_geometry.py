from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "pudge" / "web" / "review_gate.css").read_text(encoding="utf-8")


def test_closed_and_revealed_states_reserve_the_same_stage_height() -> None:
    assert "/* G10c fixed card geometry + aligned answer controls */" in CSS
    assert "--pudge-review-stage-height:clamp(" in CSS
    assert "height:var(--pudge-review-stage-height)!important" in CSS
    assert "padding-bottom:0!important" in CSS
    assert "height:var(--pudge-review-word-slot-height)!important" in CSS
    assert "height:calc(var(--pudge-review-stage-height) - var(--pudge-review-word-slot-height))" in CSS
    assert "min-height:calc(var(--pudge-review-stage-height) - var(--pudge-review-word-slot-height))" in CSS
    assert "max-height:calc(var(--pudge-review-stage-height) - var(--pudge-review-word-slot-height))" in CSS


def test_show_answer_and_grade_controls_share_the_bottom_control_line() -> None:
    assert ".pudge-review-gate-show-answer{" in CSS
    assert "--pudge-review-control-height:50px" in CSS
    assert "margin-top:auto" in CSS
    assert "flex:0 0 var(--pudge-review-control-height)" in CSS
    assert ".pudge-review-gate-back .pudge-review-gate-grades{" in CSS
    assert "position:sticky" in CSS
    assert "bottom:0" in CSS
    assert ".pudge-review-gate-grades button{min-height:var(--pudge-review-control-height)}" in CSS


def test_existing_stable_word_and_ruby_layout_contracts_remain() -> None:
    assert "/* G10 stable word slot + optimistic previous-review undo */" in CSS
    assert "/* G10b compact revealed ruby/pitch layout */" in CSS
    assert ".pudge-review-gate-card.answer-shown .pudge-review-gate-front{display:flex!important}" in CSS
