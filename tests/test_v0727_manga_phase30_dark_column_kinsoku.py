from __future__ import annotations

from pudge.manga_ocr_worker import _rebalance_dark_column_line_starts


def test_dark_column_does_not_start_inside_katakana_prolonged_sound() -> None:
    text = "甲" * 26 + "ゴールド・ロジャー"

    counts = _rebalance_dark_column_line_starts(text, [4, 10, 5, 8, 8])

    assert counts == [4, 10, 5, 7, 9]
    offset = sum(counts[:-1])
    assert text[offset:] == "ゴールド・ロジャー"
    assert sum(counts) == len(text)


def test_normal_dark_column_boundary_is_unchanged() -> None:
    assert _rebalance_dark_column_line_starts("甲乙丙丁戊己", [3, 3]) == [3, 3]
