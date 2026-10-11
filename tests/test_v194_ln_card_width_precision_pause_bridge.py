"""Listen together: the clock bridge after a precise chapter start, and the width of the
word card (behaviour; audit 71Б rewrite; the reader runs in node, tests/page_harness.py)."""

from __future__ import annotations

from css_support import css_rules
from pudge import reading_audio_alignment as raa
from t71g19_support import LN_PAIRED, OPEN
from t71g21_support import computed_style


def _chapter() -> dict:
    return {
        "leading_prefix_debug": {
            "recovered": True,
            "chapter_marker": True,
            "story_start": 14054.365,
            "marker_start": 14051.515,
            "precision_window_start": 14030.0,
            "precision_window_end": 14102.0,
        },
        "_runtime_reading_hints": [{"offset_start": 1, "offset_end": 2, "reading": "あ"}],
        "_runtime_precision_segments": [{"start": 14050.0, "end": 14100.0, "text": "x"}],
        "leading_prefix_start": 14054.365,
    }


def test_high_rate_post_precision_bridge_uses_end_of_following_pause(monkeypatch) -> None:
    chapter = _chapter()
    coarse = [
        {"offset": 0, "time": 14054.365},
        {"offset": 11, "time": 14057.515},
        {"offset": 142, "time": 14089.015},
        {"offset": 154.999, "time": 14090.360},
        {"offset": 154.999, "time": 14090.899},
        {"offset": 155, "time": 14090.900},
        {"offset": 162, "time": 14094.755},
    ]

    monkeypatch.setattr(
        raa,
        "_precision_reading_word_clock",
        lambda *args, **kwargs: (
            [
                {"offset": 11, "time": 14057.515},
                {"offset": 133, "time": 14086.700},
                {"offset": 135, "time": 14087.395},
                {"offset": 137, "time": 14088.075},
                {"offset": 140, "time": 14088.815},
                {"offset": 142, "time": 14089.015},
            ],
            {
                "mode": "precision_word_reading_prefix",
                "verified_through_offset": 142,
            },
        ),
    )

    refined, _count, debug = raa._runtime_precision_reading_prefix(chapter, coarse)

    assert debug and debug["verified_through_offset"] == 142
    # The 142 -> 154.999 bridge is ~9.7 chars/s and is immediately followed by
    # a 539ms encoded pause hold.  Do not race to 154.999 and wait there.
    assert {"offset": 154.999, "time": 14090.36} not in refined
    assert {"offset": 154.999, "time": 14090.899} not in refined
    assert {"offset": 155, "time": 14090.9, "wall_clock_from_previous": True} in refined
    assert debug["wall_clock_pause_bridge"] is True
    assert {"offset": 162, "time": 14094.755} in refined


def test_normal_rate_bridge_keeps_original_coarse_anchor(monkeypatch) -> None:
    chapter = _chapter()
    coarse = [
        {"offset": 0, "time": 10.0},
        {"offset": 20, "time": 14.0},
        {"offset": 30, "time": 16.5},  # 4 chars/s from precision end below
        {"offset": 40, "time": 19.0},
    ]
    monkeypatch.setattr(raa, "_chapter_recovered_story_hold", lambda _chapter: (10.0, None))
    monkeypatch.setattr(
        raa,
        "_precision_reading_word_clock",
        lambda *args, **kwargs: (
            [
                {"offset": 5, "time": 11.0},
                {"offset": 10, "time": 12.0},
                {"offset": 20, "time": 14.0},
            ],
            {"mode": "precision_word_reading_prefix", "verified_through_offset": 20},
        ),
    )
    refined, _count, _debug = raa._runtime_precision_reading_prefix(chapter, coarse)
    assert {"offset": 30, "time": 16.5} in refined


# The natural (one-line) widths of the card header's parts, which the window measures
# (dom_lite has no layout): NATURAL[class] = px. Start of a run body.
NATURAL_WIDTHS = r"""
window.NATURAL = {};
Object.defineProperty(Element.prototype, '__scrollWidth', {configurable: true,
  get() { for (const [name, width] of Object.entries(NATURAL)) if (this.classList?.contains(name)) return width; return undefined; },
  set(value) { Object.defineProperty(this, '__scrollWidth', {value, writable: true, configurable: true}); }});
"""

CARD = r"""
const card = () => document.getElementById('pudgeStudyCard');
const openCard = async natural => {
  PudgeReadingTools.study.close(); await __tick(50);
  window.NATURAL = natural;
  await __click('#lnReader [data-ln-token="0:0"]'); await __tick(200);
  const term = card().querySelector('.pudge-study-term');
  return {open: card().classList.contains('open'), layout: card().querySelector('.pudge-study-head').dataset.layout,
          width: __css(card(), 'width'), max: __css(card(), 'max-width'),
          term: Object.fromEntries(['white-space', 'overflow-wrap', 'word-break', 'min-width'].map(p => [p, __css(term, p)]))};
};
"""


def test_word_card_keeps_its_width_and_grows_up_to_620px_for_long_headers(page) -> None:
    # The card of a word read together with an audiobook has Play from here / Read up to
    # here in its header. It stays 440 px wide (the old card) unless the headword,
    # actions and close need more; it grows to at most 620 px, and when they do not fit
    # in one row the actions move to their own row and the headword wraps.
    wide = page.run(
        computed_style("width", "max-width", "white-space", "overflow-wrap", "word-break", "min-width")
        + NATURAL_WIDTHS
        + "AUDIO.playing = false;"
        + OPEN
        + CARD
        + r"""
        return [await openCard({'pudge-study-term-wrap': 60, 'pudge-study-head-actions': 220, 'pudge-study-close': 30}),
                await openCard({'pudge-study-term-wrap': 200, 'pudge-study-head-actions': 300, 'pudge-study-close': 30}),
                await openCard({'pudge-study-term-wrap': 700, 'pudge-study-head-actions': 300, 'pudge-study-close': 30})];
        """,
        before=LN_PAIRED,
    )
    short, longer, longest = wide.value
    viewport_cap = "calc(100vw - 24px)"
    assert short["open"] and short["layout"] == "inline" and short["width"] == f"min(440px,{viewport_cap})"
    assert longer["layout"] == "inline" and longer["width"] == f"min(530px,{viewport_cap})"
    assert longest["layout"] == "stacked" and longest["width"] == f"min(620px,{viewport_cap})"
    assert {row["max"] for row in wide.value} == {f"min(620px,{viewport_cap})"}
    assert longest["term"] == {
        "white-space": "normal",
        "overflow-wrap": "anywhere",
        "word-break": "normal",
        "min-width": "0",
    }

    # A narrow window: actions on their own row, the card as wide as the window allows.
    narrow = page.run(
        NATURAL_WIDTHS
        + "AUDIO.playing = false;"
        + OPEN
        + r"""
        await __click('#lnReader [data-ln-token="0:0"]'); await __tick(200);
        const card = document.getElementById('pudgeStudyCard');
        return {layout: card.querySelector('.pudge-study-head').dataset.layout, width: card.style.getPropertyValue('--pudge-study-width')};
        """,
        before=LN_PAIRED,
        media={"(max-width:520px)": True},
    )
    assert narrow.value == {"layout": "stacked", "width": ""}
    small = css_rules("reading_tools.css")
    assert small.get(".pudge-study-card", "width", media="(max-width:520px)") == "calc(100vw - 16px)"
    assert small.get(".pudge-study-term", "white-space", media="(max-width:520px)") == "normal"
