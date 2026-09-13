from __future__ import annotations

from PIL import Image

from pudge.manga_ocr_worker import (
    _accepted_dark_column_surfaces,
    _align_dark_column_surfaces,
    _dark_column_slot_boundaries,
    _expand_dark_column_weak_ink,
    _recognize_dark_column_surfaces,
    _refine_dark_column_vertical_extent,
    _rebalance_dark_column_boundary_punctuation,
)


FULL_WITH_HALLUCINATION = "富名声力がかつてこの世の全てを手に入れた男・海賊王・ゴールド・ロジャー"
COLUMNS = ["富名声力", "かつてこの世の全てを", "手に入れた男", "・海賊王・", "ゴールド・ロジャー"]


def test_boundary_punctuation_moves_to_following_lane() -> None:
    assert _rebalance_dark_column_boundary_punctuation([
        "手に入れた男・",
        "海賊王・",
        "ゴールド・ロジャー",
    ]) == [
        "手に入れた男",
        "・海賊王・",
        "ゴールド・ロジャー",
    ]


def test_vertical_extent_refinement_drops_early_stray_ink_after_x_tightening() -> None:
    rows = [0] * 150
    for index in range(8, 15):
        rows[index] = 1
    for index in range(32, 135):
        rows[index] = 5
    assert _refine_dark_column_vertical_extent(
        rows,
        crop_height=150,
        lane_width=10,
        start_y=8,
        end_y=134,
        margin_y=2,
    ) == (32, 134, rows)


def test_vertical_extent_refinement_preserves_trailing_text_when_tight_x_loses_middle_glyph() -> None:
    rows = [0] * 150
    for index in range(20, 62):
        rows[index] = 5
    for index in range(114, 125):
        rows[index] = 5
    start, end, _cleaned = _refine_dark_column_vertical_extent(
        rows,
        crop_height=150,
        lane_width=10,
        start_y=20,
        end_y=124,
        margin_y=2,
    )
    assert start == 20
    assert end == 124


def test_column_consensus_removes_one_unsupported_full_crop_insertion() -> None:
    assert _accepted_dark_column_surfaces(FULL_WITH_HALLUCINATION, COLUMNS) == "".join(COLUMNS)
    assert _accepted_dark_column_surfaces(FULL_WITH_HALLUCINATION, ["無関係", "別の文章"]) == ""


def test_weak_ink_extension_keeps_thin_prolonged_mark() -> None:
    rows = [0] * 70
    for index in range(10, 31):
        rows[index] = 5
    for index in range(32, 49):
        rows[index] = 1
    assert _expand_dark_column_weak_ink(rows, 10, 30, margin_y=2) == (10, 48)


def test_slot_boundaries_follow_real_ink_valleys() -> None:
    rows = [0] * 130
    for start, stop in ((0, 20), (40, 61), (63, 82), (84, 103), (106, 125)):
        for index in range(start, stop):
            rows[index] = 8
    boundaries = _dark_column_slot_boundaries(rows, 0, 124, 5)
    assert len(boundaries) == 6
    assert boundaries[1] < 40
    assert 59 <= boundaries[2] <= 64
    assert 80 <= boundaries[3] <= 85
    assert 101 <= boundaries[4] <= 107


def test_column_crop_consensus_preserves_detected_lane_boundaries() -> None:
    image = Image.new("RGB", (200, 260), "black")
    initial = []
    for x in (0.80, 0.65, 0.50, 0.35, 0.20):
        initial.append(
            {
                "text": "仮",
                "x": x,
                "y": 0.20,
                "width": 0.08,
                "height": 0.60,
                "source": "dark-columns-v1",
            }
        )

    class ColumnModel:
        def __init__(self) -> None:
            self.index = 0
            self.current = ""
            self.sizes: list[tuple[int, int]] = []

        def __call__(self, crop: Image.Image) -> str:
            self.sizes.append(crop.size)
            # The production path first reads an inverted square crop.  v47 may
            # additionally probe the original-polarity crop for small-kana
            # evidence.  Return the same lane surface for that extra view.
            if crop.getpixel((0, 0))[0] > 128:
                self.current = COLUMNS[self.index]
                self.index += 1
            return self.current

    model = ColumnModel()
    corrected, surfaces = _recognize_dark_column_surfaces(
        model, image, initial, FULL_WITH_HALLUCINATION
    )
    assert corrected == "".join(COLUMNS)
    assert surfaces == COLUMNS
    assert len(model.sizes) >= 5
    assert sum(width == height for width, height in model.sizes) >= 5


def test_rejected_column_consensus_keeps_raw_surfaces_for_diagnostics() -> None:
    full = "富名声力かつてこの世"
    surfaces = ["富名声力", "無関係"]
    assert _accepted_dark_column_surfaces(full, surfaces) == ""


def test_lane_alignment_uses_reliable_columns_and_drops_only_hallucinated_ga() -> None:
    raw = [
        "富：名声：カ",
        "かつてこの世の全てを",
        "手に入れた男",
        "【２０１７海賊王】",
        "ゴルドロジャー",
    ]
    corrected, columns = _align_dark_column_surfaces(
        FULL_WITH_HALLUCINATION,
        raw,
        [5, 8, 5, 8, 9],
    )
    assert corrected == "".join(COLUMNS)
    assert columns == COLUMNS


def test_lane_alignment_rejects_unrelated_column_ocr() -> None:
    assert _align_dark_column_surfaces(
        FULL_WITH_HALLUCINATION,
        ["無関係", "別文章", "誤認識", "不一致", "雑音"],
        [5, 8, 5, 8, 9],
    ) == ("", [])
