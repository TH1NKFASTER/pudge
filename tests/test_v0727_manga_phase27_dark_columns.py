
from __future__ import annotations

from pudge.manga_ocr_worker import _select_dark_block_column_peaks


def test_dark_block_dedupes_two_projection_peaks_inside_one_kanji_lane() -> None:
    smoothed = [0.0] * 206
    smoothed[143] = 639.0
    smoothed[129] = 600.0
    smoothed[94] = 368.0

    # 143 and 129 are stroke maxima inside one wide kanji lane.
    # 94 is the actual neighbouring printed column.
    assert _select_dark_block_column_peaks([143, 129, 94], smoothed, 206) == [143, 94]
