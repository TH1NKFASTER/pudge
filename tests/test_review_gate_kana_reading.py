from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "pudge"
    / "web"
    / "review_gate.js"
).read_text(encoding="utf-8")


def test_kana_only_review_words_do_not_render_duplicate_reading_line() -> None:
    assert "const frontWord = String(card?.wordTextPlain || card?.wordText || rows[0]?.text || '').trim();" in SOURCE
    assert "if (!/[\\u3400-\\u9fff々]/u.test(frontWord)) return '';" in SOURCE


def test_single_bracket_ruby_reading_is_still_suppressed() -> None:
    assert "values.length === 1" in SOURCE
    assert "/\\[[^\\]]+\\]/.test(values[0])" in SOURCE
