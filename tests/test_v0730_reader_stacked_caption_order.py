"""Stacked horizontal lines of one caption are read top to bottom in the manga reader, even
when the lower line reaches further right; captions side by side are still read right to
left (behaviour, page harness; audit 71Б rewrite)."""

from __future__ import annotations

from tests.t71g06_support import horizontal, reader, vertical

# Geometry in image fractions, y from the bottom (as the backend sends it).
SPEECH = vertical("そうだね", 0.70, 0.62, 0.10, 0.30)  # top .08, right edge .80
LINE_1 = horizontal("きょうは", 0.10, 0.86, 0.30, 0.04)  # top .10, right edge .40
LINE_2 = horizontal("はれのちあめ", 0.10, 0.825, 0.32, 0.04)  # top .135 (same row band), right edge .42
SIDE_LEFT = horizontal("ひだり", 0.10, 0.34, 0.20, 0.04)  # top .62
SIDE_RIGHT = horizontal("みぎ", 0.55, 0.31, 0.20, 0.04)  # top .65, no horizontal overlap
BELOW = vertical("またね", 0.20, 0.05, 0.08, 0.15)  # top .80


def test_stacked_caption_lines_are_read_top_to_bottom(page) -> None:
    result = reader(
        page,
        "return regions().map(r => r.text);",  # the overlay in document (selection and copy) order
        regions={0: [BELOW, SIDE_LEFT, LINE_2, SPEECH, SIDE_RIGHT, LINE_1]},
    )
    # The lower caption line starts only 3.5 % lower (inside the row band where bubbles are
    # read right to left) and reaches further right; it is still read after the upper line.
    # Two captions on one row that do not overlap are read right to left.
    assert result.value == ["そうだね", "きょうは", "はれのちあめ", "みぎ", "ひだり", "またね"]
