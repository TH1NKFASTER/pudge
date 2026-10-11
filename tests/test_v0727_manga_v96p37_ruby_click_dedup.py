"""Printed furigana that OCR read as a separate narrow region is not a second clickable
word: the reader hides it from the study layer only (behaviour, page harness; synthetic
geometry, no real manga OCR; audit 71Б rewrite)."""

from __future__ import annotations

from tests.t71g06_support import reader


def _row(
    text: str, x: float, y: float, width: float, height: float, orientation: str = "vertical", **extra
) -> dict:
    return {
        "text": text,
        "raw_text": text,
        "orientation": orientation,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        **extra,
    }


MAIN = _row("学校生活", 0.44, 0.16, 0.037, 0.23)
RUBY = _row("がっこう", 0.474, 0.19, 0.012, 0.11)
DIALOGUE = _row("いそいで", 0.60, 0.16, 0.043, 0.21)

# (case, OCR regions of one page, texts that must not be clickable)
CASES = [
    ("ruby next to full text", [MAIN, RUBY, DIALOGUE], ["がっこう"]),
    (
        "two independent ruby/main pairs",
        [MAIN, RUBY, _row("先生たち", 0.22, 0.4, 0.037, 0.23), _row("せんせい", 0.254, 0.44, 0.012, 0.10)],
        ["がっこう", "せんせい"],
    ),
    ("ruby not present", [MAIN, DIALOGUE], []),
    (
        "ruby next to a single kanji stays",
        [_row("人", 0.68, 0.16, 0.037, 0.23), _row("ひと", 0.714, 0.19, 0.012, 0.11)],
        [],
    ),
    ("separate kana column", [MAIN, _row("がっこう", 0.65, 0.19, 0.012, 0.11)], []),
    ("ruby as wide as the main text", [MAIN, _row("がっこう", 0.474, 0.19, 0.034, 0.11)], []),
    ("ruby nearly as tall as the main text", [MAIN, _row("がっこう", 0.474, 0.19, 0.012, 0.21)], []),
    ("ruby outside the main text's height", [MAIN, _row("がっこう", 0.474, 0.52, 0.012, 0.11)], []),
    (
        "horizontal print is never deduplicated",
        [MAIN, _row("がっこう", 0.474, 0.19, 0.012, 0.11, "horizontal")],
        [],
    ),
    ("mixed kana and kanji is not ruby", [MAIN, _row("学こう", 0.474, 0.19, 0.012, 0.11)], []),
    ("a single kana is not ruby", [MAIN, _row("が", 0.474, 0.19, 0.012, 0.11)], []),
]

LLM = {
    "settings": {
        "llm_enabled": True,
        "llm_model": "local",
        "llm_url": "http://localhost:11434",
        "llm_provider": "ollama",
    }
}


def test_synthetic_ruby_is_not_clickable_and_ordinary_dialogue_is(page) -> None:
    result = reader(
        page,
        r"""
        const shown = [regions().map(r => r.text)];
        for (let k = 1; k < window.__book.page_count; k++) {
          await __key('ArrowLeft'); await __tick(100);                       // right-to-left: next page
          shown.push(regions().map(r => r.text));
        }
        return shown;
        """,
        regions={k: rows for k, (_name, rows, _hidden) in enumerate(CASES)},
        book={"page_count": len(CASES)},
    )
    for (name, rows, hidden), shown in zip(CASES, result.value, strict=True):
        expected = [row["text"] for row in rows if row["text"] not in hidden]
        assert sorted(shown) == sorted(expected), name


def test_hidden_ruby_stays_out_of_parsing_context_and_reading_order(page) -> None:
    result = reader(
        page,
        r"""
        const out = {shown: regions().map(r => r.text), parsed: calls('study_parse_text').map(([text]) => text)};
        const index = text => regions().find(r => r.text === text).index;
        await rightClick(index('いそいで')); await choose('explain'); await __tick(20);
        return out;
        """,
        regions={0: [MAIN, RUBY, DIALOGUE]},
        before="__api.analyze_grammar = async () => ({status: 'ok'});",
        state=LLM,
    )
    out = result.value
    assert out["shown"] == ["いそいで", "学校生活"]
    # Only real dialogue is sent for parsing.
    assert sorted(out["parsed"]) == ["いそいで", "学校生活"]
    # The page context given to the grammar explanation leaves the ruby out.
    [[sentence, context, _request]] = result.calls_of("analyze_grammar")
    assert (sentence, context) == ("いそいで", "いそいで\n学校生活")
