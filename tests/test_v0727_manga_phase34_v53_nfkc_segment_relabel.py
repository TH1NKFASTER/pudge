from __future__ import annotations

from pudge.manga_ocr_worker import _relabel_nfkc_equivalent_segment_surfaces


def _segment(text: str, x: float) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": 0.5,
        "width": 0.05,
        "height": 0.08,
        "source": "vision-accurate-range-v2",
    }


def test_relabels_ascii_digit_box_to_fullwidth_final_surface() -> None:
    piece = {
        "text": "第１話",
        "segments": [_segment("第", 0.1), _segment("1", 0.15), _segment("話", 0.2)],
    }

    repaired = _relabel_nfkc_equivalent_segment_surfaces(piece)

    assert "".join(str(item["text"]) for item in repaired["segments"]) == "第１話"
    assert repaired["segments"][1]["source"] == "vision-accurate-range-v2"
    assert repaired["segments"][1]["recognition_correction"] == "nfkc-segment-surface-relabel-v1"
    assert repaired["nfkc_segment_relabel"] is True
    assert repaired["nfkc_segment_relabel_changes"] == [
        {"index": 1, "from": "1", "to": "１"}
    ]


def test_relabels_two_digit_page_number_without_moving_boxes() -> None:
    piece = {
        "text": "１２",
        "segments": [_segment("1", 0.1), _segment("2", 0.2)],
    }
    original_geometry = [
        (item["x"], item["y"], item["width"], item["height"])
        for item in piece["segments"]
    ]

    repaired = _relabel_nfkc_equivalent_segment_surfaces(piece)

    assert [item["text"] for item in repaired["segments"]] == ["１", "２"]
    assert [
        (item["x"], item["y"], item["width"], item["height"])
        for item in repaired["segments"]
    ] == original_geometry


def test_rejects_non_nfkc_equivalent_text_change() -> None:
    piece = {
        "text": "１３",
        "segments": [_segment("1", 0.1), _segment("8", 0.2)],
    }

    assert _relabel_nfkc_equivalent_segment_surfaces(piece) == piece


def test_rejects_compatibility_fold_that_is_not_ascii_fullwidth_pair() -> None:
    piece = {
        "text": "株式会社",
        "segments": [_segment("㍿", 0.1), _segment("社", 0.2), _segment("社", 0.3), _segment("社", 0.4)],
    }

    assert _relabel_nfkc_equivalent_segment_surfaces(piece) == piece
