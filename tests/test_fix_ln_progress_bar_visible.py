"""The reading progress bar of LN/manga cards is visible (regression: the hover-area padding
was taken out of its 4 px height under the window's global border-box sizing)."""

from __future__ import annotations

import re

from css_support import css_rules


def _px(value: str | None) -> float:
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)px\s*", value or "0px")
    return float(match.group(1)) if match else 0.0


def test_progress_bar_keeps_a_visible_track_and_fill() -> None:
    rules = css_rules("index.html")
    universal = rules.declarations("*")
    bar = rules.declarations(".ln-card-progress")
    sizing = bar.get("box-sizing") or universal.get("box-sizing") or "content-box"
    padding = bar.get("padding", "0").split()
    vertical = _px(padding[0]) * 2 if padding else 0.0
    height = _px(bar.get("height"))
    content = height - vertical if sizing == "border-box" else height
    # The track (background clipped to the content box) and the fill (height:100%) are drawn
    # in the content box: it must keep its 4 px.
    assert content >= 4, (sizing, bar)
    assert rules.get(".ln-card-progress span", "height") == "100%"
