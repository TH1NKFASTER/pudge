from __future__ import annotations

from copy import deepcopy

import pudge.manga_ocr_worker as worker


def _piece(*, text: str = "で", raw: str = "で", segment_text: str = "2") -> dict[str, object]:
    return {
        "text": text,
        "raw_text": raw,
        "orientation": "horizontal",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {
                "text": segment_text,
                "orientation": "horizontal",
                "x": 0.12,
                "y": 0.18,
                "width": 0.013,
                "height": 0.01,
                "source": "vision-accurate-range-v2",
            }
        ],
    }


def test_v58_relabels_real_vision_box_when_raw_and_final_agree() -> None:
    piece = _piece()
    before = deepcopy(piece["segments"][0])

    repaired = worker._relabel_raw_verified_segment_surfaces(piece)

    assert repaired["text"] == "で"
    assert repaired["segments"][0]["text"] == "で"
    assert repaired["segments"][0]["x"] == before["x"]
    assert repaired["segments"][0]["y"] == before["y"]
    assert repaired["segments"][0]["width"] == before["width"]
    assert repaired["segments"][0]["height"] == before["height"]
    assert repaired["segments"][0]["source"] == before["source"]
    assert repaired["segments"][0]["recognition_correction"] == "raw-verified-segment-relabel-v1"
    assert repaired["raw_verified_segment_relabel"] is True
    assert repaired["raw_verified_segment_relabel_changes"] == [
        {"index": 0, "from": "2", "to": "で"}
    ]


def test_v58_rejects_when_raw_does_not_confirm_final() -> None:
    piece = _piece(raw="2")
    assert worker._relabel_raw_verified_segment_surfaces(piece) is piece


def test_v58_rejects_synthetic_or_missing_geometry() -> None:
    piece = _piece()
    piece["segments"][0]["source"] = "synthetic"
    assert worker._relabel_raw_verified_segment_surfaces(piece) is piece


def test_v58_rejects_length_mismatch() -> None:
    piece = _piece(text="いい", raw="いい", segment_text="い")
    assert worker._relabel_raw_verified_segment_surfaces(piece) is piece


def test_v58_noop_when_segment_surface_already_matches() -> None:
    piece = _piece(segment_text="で")
    assert worker._relabel_raw_verified_segment_surfaces(piece) is piece
