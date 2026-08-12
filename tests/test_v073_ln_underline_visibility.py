from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ln_underline_filter_does_not_block_enabled_marks() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert ".ln-reader.word-marks-underline .ln-word{color:inherit!important;text-decoration-line:none!important" in html
    assert ".ln-reader.word-marks-underline .ln-word.ln-study-mark-enabled{text-decoration-line:underline!important" in html
    assert ".ln-reader.word-marks-underline .ln-word{color:inherit!important;text-decoration:none!important" not in html


def test_ln_rendered_tokens_keep_state_mark_class() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "markEnabled=lnCardMatchesStates" in html
    assert "ln-study-mark-enabled" in html
