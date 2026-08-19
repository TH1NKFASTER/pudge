from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_reading_tools_exposes_direct_word_card_open() -> None:
    js = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert "async function openStudyElement(word)" in js
    assert "openElement: openStudyElement" in js
    assert "await openStudyCard({" in js


def test_manga_uses_direct_card_open_and_logs_result() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "window.PudgeReadingTools?.study?.openElement" in js
    assert "jiten_card_open" in js
    assert "jiten_card_failed" in js
    assert "token.dispatchEvent(new MouseEvent('click'" not in js


def test_manga_hit_slop_covers_tight_vision_boxes() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "const maxSnap = Math.max(18, Math.min(42" in js
    assert "pointDistanceToRect" in js
