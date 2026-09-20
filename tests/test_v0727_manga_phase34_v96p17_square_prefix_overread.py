from copy import deepcopy
from pathlib import Path

from pudge import manga_ocr_worker as worker


def _lane(*, direct: str, square: str, component_count: int, width: float = 0.03, height: float = 0.035) -> dict[str, object]:
    return {
        "text": square,
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.5,
        "y": 0.2,
        "width": width,
        "height": height,
        "confidence": 0.6,
        "detector": "manga-raw-components-v1",
        "source": worker._LAYOUT_LINE_SOURCE,
        "selected_hypothesis_id": "manga-ocr-square-retry",
        "recognizer_retry": "vertical-square-pad-v1",
        "geometry_source": "layout-line-proportional-v1",
        "provenance": {
            "proposal_kind": "vertical_text_line_raw",
            "component_count": component_count,
            "component_coverage": 0.90,
        },
        "hypotheses": [
            {"id": "manga-ocr", "text": direct, "source": "manga-ocr", "selected": False},
            {"id": "manga-ocr-square-retry", "text": square, "source": "manga-ocr", "selected": True},
        ],
        "segments": [
            {
                "text": ch,
                "orientation": "vertical",
                "x": 0.5,
                "y": 0.2 + (len(square) - i - 1) * 0.005,
                "width": 0.02,
                "height": 0.005,
                "source": "layout-line-ink-v2+tight-v1",
            }
            for i, ch in enumerate(square)
        ],
    }


def test_v96p17_reverts_p12_like_square_only_prefix() -> None:
    region = _lane(direct="ぞ！！", square="べぞ！！", component_count=4)
    repaired = worker._repair_short_punctuated_square_retry_prefix_overread(region)

    assert repaired["text"] == "ぞ！！"
    assert repaired["selected_hypothesis_id"] == "manga-ocr"
    assert repaired["recognizer_retry"] == "vertical-square-prefix-overread-revert-v1"
    assert "".join(segment["text"] for segment in repaired["segments"]) == "ぞ！！"
    assert repaired["provenance"]["square_prefix_overread_reverted"]["removed_prefix"] == "べ"


def test_v96p17_reverts_p39_like_square_only_prefix() -> None:
    region = _lane(direct="や！！", square="レや！！", component_count=2)
    repaired = worker._repair_short_punctuated_square_retry_prefix_overread(region)
    assert repaired["text"] == "や！！"


def test_v96p17_does_not_trim_non_punctuated_or_longer_dialogue() -> None:
    non_punctuated = _lane(direct="やる", square="レやる", component_count=3)
    longer = _lane(direct="やる！！", square="レやる！！", component_count=4)
    assert worker._repair_short_punctuated_square_retry_prefix_overread(non_punctuated) == non_punctuated
    assert worker._repair_short_punctuated_square_retry_prefix_overread(longer) == longer


def test_v96p17_does_not_trim_other_detector_or_real_raw_text() -> None:
    region = _lane(direct="や！！", square="レや！！", component_count=2)
    other = deepcopy(region)
    other["detector"] = worker._LAYOUT_DETECTOR
    raw = deepcopy(region)
    raw["raw_text"] = "レや！！"
    assert worker._repair_short_punctuated_square_retry_prefix_overread(other) == other
    assert worker._repair_short_punctuated_square_retry_prefix_overread(raw) == raw


def test_v96p17_generation_markers_advance() -> None:
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    source = Path(worker.__file__).read_text(encoding="utf-8")
    assert "square-prefix-overread-revert-v1" in source
