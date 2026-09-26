from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "pudge" / "web" / "review_gate.css").read_text(encoding="utf-8")


def test_revealed_word_slot_collapses_without_hiding_word():
    assert "/* G10b compact revealed ruby/pitch layout */" in CSS
    assert ".pudge-review-gate-card.answer-shown .pudge-review-gate-front{" in CSS
    assert "min-height:70px!important" in CSS
    assert "padding:18px 12px 0!important" in CSS
    # Existing G10 behaviour must remain: the word itself stays on screen.
    assert ".pudge-review-gate-card.answer-shown .pudge-review-gate-front{display:flex!important}" in CSS


def test_revealed_ruby_and_pitch_are_visually_grouped():
    assert ".pudge-review-gate-front .pudge-review-gate-word ruby{ruby-align:center}" in CSS
    assert ".pudge-review-gate-front .pudge-review-gate-word rt{line-height:1;letter-spacing:0}" in CSS
    assert ".pudge-review-gate-card.answer-shown .pudge-review-gate-pitch{" in CSS
    assert "justify-content:center" in CSS
    assert "margin-top:0" in CSS
