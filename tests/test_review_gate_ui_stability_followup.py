from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


def test_loading_gate_is_not_made_translucent() -> None:
    css = (WEB / "review_gate.css").read_text(encoding="utf-8")
    assert ".pudge-review-gate-busy{cursor:progress}" in css
    assert ".pudge-review-gate-busy{pointer-events:none}" not in css
    assert ".pudge-review-gate-busy{opacity:" not in css
    assert "backdrop-filter:blur(" not in css


def test_grade_hover_does_not_create_extra_compositor_effects() -> None:
    gate_css = (WEB / "review_gate.css").read_text(encoding="utf-8")
    study_css = (WEB / "reading_tools.css").read_text(encoding="utf-8")
    assert "pudge-review-gate-card{isolation:isolate}" in gate_css
    assert "pudge-review-gate-grades button:hover" in gate_css
    assert "transform:none!important" in gate_css
    assert "filter:none!important" in gate_css
    assert "transition:none!important" in gate_css
    assert "box-shadow:none!important" in gate_css
    assert "pudge-study-grade:hover" in study_css
    assert "box-shadow:none!important" in study_css


def test_review_gate_blurs_old_grade_before_dom_replacement() -> None:
    source = (WEB / "review_gate.js").read_text(encoding="utf-8")
    assert "const focusedBeforeRender = document.activeElement;" in source
    assert "focusedBeforeRender.blur?.();" in source
    assert "const focusedGrade = document.activeElement?.closest?.('[data-review-gate-grade]');" in source
    assert "focusedGrade.blur?.();" in source


def test_single_bracket_ruby_reading_is_not_rendered_twice() -> None:
    from test_review_gate_kana_reading import line
    assert line({"wordTextPlain":"伏せる","readingIndex":0,"readings":[{"text":"伏[ふ]せる","readingIndex":0}]}) == ""


def test_normal_jiten_card_blurs_focus_before_close() -> None:
    source = (WEB / "reading_tools.js").read_text(encoding="utf-8")
    assert "const focusedInside = document.activeElement;" in source
    assert "card.contains(focusedInside)" in source
    assert "focusedInside.blur?.();" in source
