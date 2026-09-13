from __future__ import annotations

from PIL import Image

from pudge.manga_ocr_worker import (
    _dark_column_slot_boundaries,
    _strip_dark_column_horizontal_rules,
)


def test_local_panel_rule_is_removed_before_vertical_slotting() -> None:
    rows = [0] * 260
    # A partial-width narration-frame rule: almost the entire 25 px lane is
    # white for a long band, but the page-wide filter would not see it.
    for row in range(8, 54):
        rows[row] = 25 if row not in {30, 31, 33, 34, 39, 42, 44, 49} else 0
    # Five real vertical glyph/punctuation runs below the frame.
    for start, stop, ink in (
        (71, 80, 4),
        (81, 101, 11),
        (102, 121, 12),
        (123, 141, 10),
        (143, 152, 4),
    ):
        for row in range(start, stop):
            rows[row] = ink

    cleaned = _strip_dark_column_horizontal_rules(rows, 25)
    assert all(cleaned[row] == 0 for row in range(8, 54))
    assert any(cleaned[row] for row in range(71, 152))

    boundaries = _dark_column_slot_boundaries(cleaned, 71, 151, 5)
    assert boundaries[0] == 71
    assert 79 <= boundaries[1] <= 82
    assert 99 <= boundaries[2] <= 103
    assert 119 <= boundaries[3] <= 123
    assert 139 <= boundaries[4] <= 143
    assert boundaries[-1] == 152


def test_short_dense_kanji_stroke_is_not_treated_as_panel_rule() -> None:
    rows = [0] * 120
    for row in range(20, 90):
        rows[row] = 6
    rows[48] = 24
    rows[49] = 24

    cleaned = _strip_dark_column_horizontal_rules(rows, 25)
    assert cleaned == rows


def test_p004_like_king_lane_excludes_local_top_rule() -> None:
    image = Image.new("RGB", (200, 260), "black")
    # White partial frame and five vertical glyph slots in the fourth lane.
    for x in range(65, 90):
        for y in range(8, 54):
            image.putpixel((x, y), (255, 255, 255))
    for start, stop in ((71, 80), (81, 101), (102, 121), (123, 141), (143, 152)):
        for x in range(72, 82):
            for y in range(start, stop):
                image.putpixel((x, y), (255, 255, 255))

    # This unit deliberately exercises the helper contract rather than the
    # full peak detector, which needs several independent lanes.
    rows = [
        sum(1 for x in range(65, 90) if image.getpixel((x, y))[0] > 128)
        for y in range(260)
    ]
    cleaned = _strip_dark_column_horizontal_rules(rows, 25)
    active = [index for index, value in enumerate(cleaned) if value]
    assert min(active) == 71
    assert max(active) == 151
