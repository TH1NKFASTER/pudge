from __future__ import annotations

from pudge.manga_ocr_worker import _dark_column_cell_bounds


def test_dark_column_hitbox_centers_on_main_glyph_not_projection_peak() -> None:
    # Densest strokes can put the projection peak left or right of the actual
    # main-glyph envelope. The hitbox must follow the recovered envelope.
    assert _dark_column_cell_bounds(
        center=134,
        glyph_left=130,
        glyph_right=146,
        start_x=125,
        end_x=146,
        target_width=16,
    ) == (130, 146)
    assert _dark_column_cell_bounds(
        center=116,
        glyph_left=105,
        glyph_right=121,
        start_x=105,
        end_x=126,
        target_width=16,
    ) == (105, 121)


def test_dark_column_hitbox_stays_inside_lane_at_edge() -> None:
    assert _dark_column_cell_bounds(
        center=20,
        glyph_left=9,
        glyph_right=19,
        start_x=10,
        end_x=30,
        target_width=16,
    ) == (10, 26)
